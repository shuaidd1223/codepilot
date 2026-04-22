"""Project-resolution policies shared by natural-language auto entrypoints."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import click

from codepilot import db
from codepilot.config import find_config, load_config

TEMP_SESSION_NAME = "公共临时会话"


def _is_subpath(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def _is_temporary_workspace(path: Path) -> bool:
    """Cross-platform temporary workspace probe."""
    import tempfile

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


def resolve_project_for_prompt(
    project: Optional[str] = None,
    cwd: Optional[Path] = None,
    *,
    auto_register: bool = True,
    allow_temporary: bool = False,
    require_registered: bool = False,
    is_temporary_workspace_fn: Optional[Callable[[Path], bool]] = None,
) -> dict:
    """Resolve target project context for auto/go/chat entrypoints."""
    db.init_db()
    current_dir = Path(cwd or Path.cwd()).resolve()
    is_temporary_workspace = is_temporary_workspace_fn or _is_temporary_workspace

    if project:
        proj = db.get_project(project)
        if not proj:
            raise click.ClickException(f"项目 '{project}' 未注册")
        return proj

    config_path = find_config(current_dir)
    if config_path:
        cfg = load_config(config_path)
        project_root = config_path.parent.resolve()
        matched = db.find_project_by_path(project_root)
        project_name = (cfg.project_name or cfg.project.name or project_root.name) if cfg else project_root.name
        if matched and Path(matched["path"]).resolve() == project_root:
            if not auto_register or require_registered:
                return matched
            return db.register_project(
                name=matched["name"],
                path=str(project_root),
                base_branch=(cfg.base_branch if cfg else matched.get("base_branch", "dev")),
                default_mode=(cfg.default_mode if cfg else matched.get("default_mode", "dual")),
                worktree_base=(cfg.worktree_base if cfg else matched.get("worktree_base")),
                config_file=str(config_path),
            )

        if auto_register and not require_registered:
            return db.register_project(
                name=project_name,
                path=str(project_root),
                base_branch=(cfg.base_branch if cfg else "dev"),
                default_mode=(cfg.default_mode if cfg else "dual"),
                worktree_base=(cfg.worktree_base if cfg else None),
                config_file=str(config_path),
            )

        if allow_temporary and is_temporary_workspace(current_dir):
            return _build_temporary_session(current_dir)
        raise click.ClickException(_register_guidance(project_root))

    matched_current = db.find_project_by_path(current_dir)
    if matched_current:
        return matched_current

    if auto_register and not require_registered and (current_dir / ".git").exists():
        return db.register_project(
            name=current_dir.name,
            path=str(current_dir),
            base_branch="main",
            default_mode="dual",
            config_file=None,
        )

    if allow_temporary and is_temporary_workspace(current_dir):
        return _build_temporary_session(current_dir)

    if require_registered or not auto_register:
        raise click.ClickException(_register_guidance(current_dir))
    raise click.ClickException("未找到当前项目，请先运行 codepilot init，或在命令里显式指定 --project")
