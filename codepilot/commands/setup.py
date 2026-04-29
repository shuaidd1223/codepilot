"""Project-level setup command for CodePilot."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core import config as config_mod
from codepilot.core import event_plugins
from codepilot.core import skill_catalog
from codepilot.core.output import echo, safe
from codepilot.storage import database as db

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


PROJECT_DIRECTORIES = (
    ".codepilot",
    ".codepilot/state",
    ".codepilot/specs",
    ".codepilot/plans",
    ".codepilot/wiki",
    ".codepilot/hooks",
    ".codepilot/events",
    ".codepilot/exec",
    ".codepilot/skills",
)


class SetupError(ValueError):
    """Raised when setup cannot safely continue."""


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _action(kind: str, path: Path, root: Path, status: str, detail: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": kind,
        "relative_path": _relative(path, root),
        "absolute_path": str(path.resolve()),
        "status": status,
    }
    if detail:
        item["detail"] = detail
    return item


def _validate_project_path(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise SetupError(f"项目路径无效：{path}") from exc
    if not resolved.exists() or not resolved.is_dir():
        raise SetupError(f"项目路径不存在或不是目录：{resolved}")
    return resolved


def _load_existing_config(config_path: Path) -> dict[str, Any]:
    try:
        with open(config_path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception as exc:
        raise SetupError(f"AGENTS.toml 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise SetupError("AGENTS.toml 顶层结构必须是 TOML table。")
    return data


def _setup_config(root: Path, project_name: str, *, dry_run: bool) -> list[dict[str, Any]]:
    config_path = root / config_mod.CONFIG_FILENAME
    if config_path.exists():
        data = _load_existing_config(config_path)
        from codepilot.commands import config_cmd

        canonical = config_cmd._canonical_config(data, project_name=project_name)
        content = config_cmd.render_agents_toml(canonical)
        current = config_path.read_text(encoding="utf-8", errors="replace")

        sync_secrets = config_cmd._extract_sync_secrets(data)
        secrets_path = config_path.parent / config_mod.SECRETS_FILENAME
        existing_secrets = config_cmd._canonical_secrets(config_cmd._load_toml_dict(secrets_path))
        existing_feishu_secret = ""
        existing_feishu = existing_secrets.get("feishu_bot")
        if isinstance(existing_feishu, dict):
            existing_feishu_secret = str(existing_feishu.get("app_secret", "") or "").strip()
        write_secrets = bool(sync_secrets) and not existing_feishu_secret

        actions: list[dict[str, Any]] = []
        if current == content:
            actions.append(_action("config", config_path, root, "exists", "AGENTS.toml 已是最新规范格式。"))
        elif dry_run:
            actions.append(_action("config", config_path, root, "would_refresh", "将按 config sync 规则刷新项目配置。"))
        else:
            config_path.write_text(content, encoding="utf-8")
            actions.append(_action("config", config_path, root, "refreshed", "已按 config sync 规则刷新项目配置。"))

        if write_secrets:
            if dry_run:
                actions.append(_action("secrets", secrets_path, root, "would_create", "将迁移内联飞书 App Secret。"))
            else:
                merged_secrets = config_cmd._merge_secret_dicts(existing_secrets, sync_secrets)
                secrets_path.write_text(config_cmd.render_secrets_toml(merged_secrets), encoding="utf-8")
                actions.append(_action("secrets", secrets_path, root, "created", "已迁移内联飞书 App Secret。"))
        return actions

    if dry_run:
        return [_action("config", config_path, root, "would_create", "将生成项目配置。")]

    from codepilot.commands import config_cmd

    canonical = config_cmd._canonical_config({}, project_name=project_name)
    config_path.write_text(config_cmd.render_agents_toml(canonical), encoding="utf-8")
    return [_action("config", config_path, root, "created", "已生成项目配置。")]


def _setup_directories(root: Path, *, dry_run: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for relative in PROJECT_DIRECTORIES:
        path = root / relative
        if path.is_dir():
            actions.append(_action("directory", path, root, "exists"))
            continue
        if dry_run:
            actions.append(_action("directory", path, root, "would_create"))
            continue
        path.mkdir(parents=True, exist_ok=True)
        actions.append(_action("directory", path, root, "created"))
    return actions


def _setup_event_registry(root: Path, *, dry_run: bool) -> dict[str, Any]:
    result = event_plugins.ensure_event_registry(root, dry_run=dry_run)
    return _action(
        "event_registry",
        Path(result["path"]),
        root,
        str(result["status"]),
        "项目本地事件 sink registry。",
    )


def _setup_skill_catalog(root: Path, *, dry_run: bool) -> dict[str, Any]:
    result = skill_catalog.ensure_skill_catalog(root, dry_run=dry_run)
    return _action(
        "skill_catalog",
        Path(result["path"]),
        root,
        str(result["status"]),
        "项目本地 skill catalog。",
    )


def _setup_registration(root: Path, project_name: str, *, dry_run: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    config_path = root / config_mod.CONFIG_FILENAME
    project_info = {
        "name": project_name,
        "path": str(root),
        "config_file": str(config_path.resolve()),
    }
    if dry_run:
        return project_info, _action("project", root, root, "would_register", "将注册或刷新项目数据库记录。")

    db.init_db()
    existing = db.find_project_by_path(root)
    if existing and Path(existing["path"]).resolve() != root:
        existing = None
    if not existing:
        existing = db.get_project(project_name)

    if existing:
        project_info = dict(existing)
        return project_info, _action("project", root, root, "exists", "项目数据库记录已存在。")

    registered = db.register_project(
        project_name,
        str(root),
        base_branch="dev",
        default_mode="dual",
        config_file=str(config_path.resolve()),
    )
    return registered, _action("project", root, root, "registered", "已注册项目数据库记录。")


def setup_project(path: Path, project_name: str | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Prepare project-local CodePilot state without installing real Codex hooks."""
    root = _validate_project_path(path)
    resolved_name = (project_name or root.name).strip()
    if not resolved_name:
        raise SetupError("项目名称不能为空。")

    actions: list[dict[str, Any]] = []
    actions.extend(_setup_config(root, resolved_name, dry_run=dry_run))
    actions.extend(_setup_directories(root, dry_run=dry_run))
    actions.append(_setup_event_registry(root, dry_run=dry_run))
    actions.append(_setup_skill_catalog(root, dry_run=dry_run))
    project_info, project_action = _setup_registration(root, resolved_name, dry_run=dry_run)
    actions.append(project_action)
    actions.append(
        _action(
            "codex_hooks",
            root / ".codex" / "hooks.json",
            root,
            "skipped",
            "第一阶段仅准备 .codepilot/hooks，不修改真实 .codex/hooks.json。",
        )
    )
    return {
        "dry_run": dry_run,
        "project": {
            "name": project_info.get("name") or resolved_name,
            "path": str(root),
            "config_file": str((root / config_mod.CONFIG_FILENAME).resolve()),
        },
        "actions": actions,
    }


@click.command("setup")
@click.argument("path", required=False, default=".", type=click.Path(file_okay=False, path_type=Path))
@click.option("--name", "-n", "project_name", help="项目名称（默认取目录名）")
@click.option("--dry-run", is_flag=True, help="只报告将执行的初始化动作，不写入文件或数据库")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def setup(ctx: click.Context, path: Path, project_name: str | None, dry_run: bool, json_mode: bool) -> None:
    """初始化项目级 .codepilot 骨架、配置和项目注册记录。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        data = setup_project(path, project_name, dry_run=dry_run)
    except SetupError as exc:
        if json_mode:
            emit_json_payload("setup", ok=False, data={}, error=str(exc), error_code="setup_error")
            ctx.exit(1)
        raise click.ClickException(str(exc)) from exc

    if json_mode:
        emit_json_payload("setup", ok=True, data=data)
        return

    echo("[bold]codepilot setup[/bold]  项目初始化")
    echo(f"  项目: {safe(data['project']['name'])}")
    echo(f"  路径: {safe(data['project']['path'])}")
    if dry_run:
        echo("  模式: dry-run")
    for item in data["actions"]:
        echo(f"  {safe(item['status']):14s} {safe(item['relative_path'])}")
