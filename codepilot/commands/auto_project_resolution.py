"""Project-resolution policies shared by natural-language auto entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
import tempfile
from pathlib import Path
from typing import Optional

import click

from codepilot.storage import database as db
from codepilot.core.config import ConfigError, find_config, load_config

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


def _auto_fix_config_error(config_path: Path, error: ConfigError) -> bool:
    """检测到 AGENTS.toml 配置错误时，提议自动修复。

    提示用户执行 `config sync` 来修复（移除废弃字段、补充默认值）。
    仅在交互式终端下询问，非交互环境直接返回 False。

    Returns:
        True 表示用户确认且修复成功；False 表示跳过或修复失败。
    """
    from codepilot.core.output import echo

    echo()
    echo(f"[red]配置错误:[/red] {error.args[0] if error.args else error}")
    echo()

    if not click.get_text_stream("stdin").isatty():
        echo("[yellow]非交互环境，跳过自动修复。请手动执行:[/yellow]")
        echo(f"  [bold]codepilot config sync {config_path.parent}[/bold]")
        echo()
        return False

    confirmed = click.confirm(
        "是否需要自动执行配置同步来修复此问题？（将移除废弃字段、补充默认配置项）",
        default=True,
    )
    if not confirmed:
        echo("[yellow]已取消自动修复，请手动执行:[/yellow]")
        echo(f"  [bold]codepilot config sync {config_path.parent}[/bold]")
        echo()
        return False

    return _apply_config_sync(config_path)


def _apply_config_sync(config_path: Path) -> bool:
    """执行 `codepilot config sync` 等效操作，修复指定 AGENTS.toml。"""
    import tomllib

    from codepilot.commands.config_cmd import _canonical_config, render_agents_toml
    from codepilot.core.output import echo

    echo("[cyan]正在修复配置...[/cyan]")
    try:
        project_root = config_path.parent
        raw_data = {}
        if config_path.exists():
            with open(config_path, "rb") as handle:
                raw_data = tomllib.load(handle)
        canonical = _canonical_config(raw_data, project_name=project_root.name)
        content = render_agents_toml(canonical)
        config_path.write_text(content, encoding="utf-8")
        echo(f"[green]已修复 {config_path}，继续执行...[/green]")
        return True
    except Exception as exc:
        echo(f"[red]配置同步失败: {exc}[/red]")
        echo("[yellow]请手动执行 codepilot config sync 后再重试。[/yellow]")
        return False


def _resolve_from_config_strategy(
    *,
    current_dir: Path,
    policy: _ResolutionPolicy,
) -> Optional[dict]:
    config_path = find_config(current_dir)
    if not config_path:
        return None

    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        if _auto_fix_config_error(config_path, exc):
            try:
                cfg = load_config(config_path)
            except ConfigError as retry_exc:
                raise click.ClickException(
                    "配置同步后仍存在错误: " + (retry_exc.args[0] if retry_exc.args else str(retry_exc))
                ) from retry_exc
        else:
            raise click.ClickException(str(exc)) from exc

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

