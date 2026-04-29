"""Safe hook wrapper planning commands."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core import hook_registry
from codepilot.core.output import echo, safe
from codepilot.storage import database as db


def _resolve_project(project: str | None) -> dict:
    db.init_db()
    if project:
        found = db.get_project(project)
        if not found:
            raise click.ClickException(f"项目 '{project}' 未注册。")
        return found
    found = db.find_project_by_path(Path.cwd())
    if not found:
        raise click.ClickException("当前目录不属于已注册项目；请使用 -p/--project 指定项目。")
    return found


def _emit_error(ctx: click.Context, command: str, json_mode: bool, exc: Exception) -> None:
    if json_mode:
        emit_json_payload(command, ok=False, data={}, error=str(exc), error_code="hook_error")
        ctx.exit(1)
    raise click.ClickException(str(exc))


@click.group("hook")
def hook_group() -> None:
    """规划项目级 hook wrapper；默认不修改真实 Codex hooks。"""


@hook_group.command("plan")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def plan_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """输出 hook wrapper 安装计划。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        data = {"project": project_info["name"], **hook_registry.plan_hooks(project_info["path"])}
    except (hook_registry.HookRegistryError, click.ClickException) as exc:
        _emit_error(ctx, "hook plan", json_mode, exc)
        return
    if json_mode:
        emit_json_payload("hook plan", ok=True, data=data)
        return
    echo(f"[cyan]hook plan[/cyan] {safe(project_info['name'])}")
    echo(f"registry: {safe(data['registry_path'])}")
    echo(f"codex hooks: {safe(data['codex_hooks']['status'])}")


@hook_group.command("install")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--dry-run", is_flag=True, help="只输出计划，不写文件")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def install_cmd(ctx: click.Context, project: str | None, dry_run: bool, json_mode: bool) -> None:
    """规划 hook wrapper 安装；当前只允许 dry-run。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        data = {"project": project_info["name"], **hook_registry.install_plan(project_info["path"], dry_run=dry_run)}
    except (hook_registry.HookRegistryError, click.ClickException) as exc:
        _emit_error(ctx, "hook install", json_mode, exc)
        return
    if json_mode:
        emit_json_payload("hook install", ok=True, data=data)
        return
    echo("[green]hook install dry-run 完成[/green]")


@hook_group.command("uninstall")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--dry-run", is_flag=True, help="只输出计划，不写文件")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def uninstall_cmd(ctx: click.Context, project: str | None, dry_run: bool, json_mode: bool) -> None:
    """规划 hook wrapper 卸载；当前只允许 dry-run。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        data = {"project": project_info["name"], **hook_registry.uninstall_plan(project_info["path"], dry_run=dry_run)}
    except (hook_registry.HookRegistryError, click.ClickException) as exc:
        _emit_error(ctx, "hook uninstall", json_mode, exc)
        return
    if json_mode:
        emit_json_payload("hook uninstall", ok=True, data=data)
        return
    echo("[green]hook uninstall dry-run 完成[/green]")
