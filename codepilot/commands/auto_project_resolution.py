"""Project-resolution policies shared by natural-language auto entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
import tempfile
from pathlib import Path
from typing import Optional

import click

from codepilot.storage import database as db
from codepilot.core.config import find_config, load_config

TEMP_SESSION_NAME = "公共临时会话"


@dataclass(frozen=True)
class _ResolutionPolicy:
    auto_register: bool = True
    allow_temporary: bool = False
    require_registered: bool = False


def _is_subpath(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def _is_temporary_workspace(path: Path) -> bool:
    """Cross-platform temporary workspace probe."""
    home = Path.home().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    return _is_subpath(path, home) or _is_subpath(path, temp_root)


def _build_temporary_session(path: Path) -> dict:
    resolved = path.resolve()
    return {
        "name": TEMP_SESSION_NAME,
        "path": str(resolved),
        "base_branch": "",
        "default_mode": "dual",
        "worktree_base": None,
        "config_file": None,
        "is_temporary": True,
    }


def _register_guidance(path: Path) -> str:
    resolved = path.resolve()
    return (
        "当前路径不在已注册项目中，需求/任务执行前请先注册项目。\n"
        f"建议先执行: codepilot init \"{resolved}\""
    )


def _resolve_explicit_project(project: Optional[str]) -> Optional[dict]:
    if not project:
        return None
    proj = db.get_project(project)
    if not proj:
        raise click.ClickException(f"项目 '{project}' 未注册")
    return proj


def _register_project_from_config(
    *,
    name: str,
    project_root: Path,
    config_path: Path,
    base_branch: str,
    default_mode: str,
    worktree_base: Optional[str],
) -> dict:
    return db.register_project(
        name=name,
        path=str(project_root),
        base_branch=base_branch,
        default_mode=default_mode,
        worktree_base=worktree_base,
        config_file=str(config_path),
    )


def _resolve_with_existing_config_project(
    *,
    matched: dict,
    cfg,
    policy: _ResolutionPolicy,
    project_root: Path,
    config_path: Path,
) -> dict:
    if not policy.auto_register or policy.require_registered:
        return matched
    return _register_project_from_config(
        name=matched["name"],
        project_root=project_root,
        config_path=config_path,
        base_branch=(cfg.base_branch if cfg else matched.get("base_branch", "dev")),
        default_mode=(cfg.default_mode if cfg else matched.get("default_mode", "dual")),
        worktree_base=(cfg.worktree_base if cfg else matched.get("worktree_base")),
    )


def _resolve_with_new_config_project(
    *,
    cfg,
    policy: _ResolutionPolicy,
    project_root: Path,
    config_path: Path,
    current_dir: Path,
) -> dict:
    if policy.auto_register and not policy.require_registered:
        project_name = (cfg.project_name or cfg.project.name or project_root.name) if cfg else project_root.name
        return _register_project_from_config(
            name=project_name,
            project_root=project_root,
            config_path=config_path,
            base_branch=(cfg.base_branch if cfg else "dev"),
            default_mode=(cfg.default_mode if cfg else "dual"),
            worktree_base=(cfg.worktree_base if cfg else None),
        )
    if policy.allow_temporary and _is_temporary_workspace(current_dir):
        return _build_temporary_session(current_dir)
    raise click.ClickException(_register_guidance(project_root))


def _resolve_from_config_strategy(
    *,
    current_dir: Path,
    policy: _ResolutionPolicy,
) -> Optional[dict]:
    config_path = find_config(current_dir)
    if not config_path:
        return None

    cfg = load_config(config_path)
    project_root = config_path.parent.resolve()
    matched = db.find_project_by_path(project_root)
    if matched and Path(matched["path"]).resolve() == project_root:
        return _resolve_with_existing_config_project(
            matched=matched,
            cfg=cfg,
            policy=policy,
            project_root=project_root,
            config_path=config_path,
        )
    return _resolve_with_new_config_project(
        cfg=cfg,
        policy=policy,
        project_root=project_root,
        config_path=config_path,
        current_dir=current_dir,
    )


def _resolve_from_workspace_strategy(
    *,
    current_dir: Path,
    policy: _ResolutionPolicy,
) -> dict:
    matched_current = db.find_project_by_path(current_dir)
    if matched_current:
        return matched_current

    if policy.auto_register and not policy.require_registered and (current_dir / ".git").exists():
        return db.register_project(
            name=current_dir.name,
            path=str(current_dir),
            base_branch="main",
            default_mode="dual",
            config_file=None,
        )

    if policy.allow_temporary and _is_temporary_workspace(current_dir):
        return _build_temporary_session(current_dir)

    if policy.require_registered or not policy.auto_register:
        raise click.ClickException(_register_guidance(current_dir))
    raise click.ClickException("未找到当前项目，请先运行 codepilot init，或在命令里显式指定 --project")


def resolve_project_for_prompt(
    project: Optional[str] = None,
    cwd: Optional[Path] = None,
    *,
    auto_register: bool = True,
    allow_temporary: bool = False,
    require_registered: bool = False,
) -> dict:
    """Resolve target project context for auto/go/chat entrypoints."""
    db.init_db()
    policy = _ResolutionPolicy(
        auto_register=auto_register,
        allow_temporary=allow_temporary,
        require_registered=require_registered,
    )
    current_dir = Path(cwd or Path.cwd()).resolve()

    explicit_project = _resolve_explicit_project(project)
    if explicit_project:
        return explicit_project

    from_config = _resolve_from_config_strategy(
        current_dir=current_dir,
        policy=policy,
    )
    if from_config:
        return from_config

    return _resolve_from_workspace_strategy(
        current_dir=current_dir,
        policy=policy,
    )

