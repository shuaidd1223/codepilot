"""Project-local event sink commands."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core import event_plugins
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
        emit_json_payload(command, ok=False, data={}, error=str(exc), error_code="event_error")
        ctx.exit(1)
    raise click.ClickException(str(exc))


@click.group("event")
def event_group() -> None:
    """管理项目本地事件 sink。"""


@event_group.command("list")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def list_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """列出事件 sink registry。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        registry = event_plugins.ensure_event_registry(project_info["path"])["registry"]
    except (event_plugins.EventPluginError, click.ClickException) as exc:
        _emit_error(ctx, "event list", json_mode, exc)
        return

    data = {"project": project_info["name"], "registry_path": str(event_plugins.registry_path(project_info["path"])), **registry}
    if json_mode:
        emit_json_payload("event list", ok=True, data=data)
        return
    if not data["sinks"]:
        echo("[yellow]暂无事件 sink[/yellow]")
        return
    for sink in data["sinks"]:
        click.echo(
            f"{sink['name']}\t{sink['type']}\t{'enabled' if sink.get('enabled') else 'disabled'}\t{sink['path']}"
        )


@event_group.command("register")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--name", required=True, help="sink 名称")
@click.option("--type", "sink_type", type=click.Choice(["jsonl"], case_sensitive=False), default="jsonl", show_default=True)
@click.option("--path", "sink_path", required=True, help="项目内 JSONL 输出路径")
@click.option("--event", "events", multiple=True, help="订阅事件类型，可重复；默认 *")
@click.option("--disable", is_flag=True, help="注册但默认禁用")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def register_cmd(
    ctx: click.Context,
    project: str | None,
    name: str,
    sink_type: str,
    sink_path: str,
    events: tuple[str, ...],
    disable: bool,
    json_mode: bool,
) -> None:
    """注册或覆盖一个本地事件 sink。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        if sink_type.lower() != "jsonl":
            raise event_plugins.EventPluginError(f"暂不支持事件 sink 类型：{sink_type}")
        sink = event_plugins.register_jsonl_sink(
            project_info["path"],
            name=name,
            path=sink_path,
            events=list(events) or ["*"],
            enabled=not disable,
        )
    except (event_plugins.EventPluginError, click.ClickException) as exc:
        _emit_error(ctx, "event register", json_mode, exc)
        return

    data = {"project": project_info["name"], "sink": sink}
    if json_mode:
        emit_json_payload("event register", ok=True, data=data)
        return
    echo(f"[green][OK] 已注册事件 sink：{safe(sink['name'])}[/green]")


@event_group.command("enable")
@click.argument("name")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def enable_cmd(ctx: click.Context, name: str, project: str | None, json_mode: bool) -> None:
    """启用一个事件 sink。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        sink = event_plugins.set_sink_enabled(project_info["path"], name, True)
    except (event_plugins.EventPluginError, click.ClickException) as exc:
        _emit_error(ctx, "event enable", json_mode, exc)
        return
    data = {"project": project_info["name"], "sink": sink}
    if json_mode:
        emit_json_payload("event enable", ok=True, data=data)
        return
    echo(f"[green][OK] 已启用事件 sink：{safe(sink['name'])}[/green]")


@event_group.command("disable")
@click.argument("name")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def disable_cmd(ctx: click.Context, name: str, project: str | None, json_mode: bool) -> None:
    """停用一个事件 sink。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        sink = event_plugins.set_sink_enabled(project_info["path"], name, False)
    except (event_plugins.EventPluginError, click.ClickException) as exc:
        _emit_error(ctx, "event disable", json_mode, exc)
        return
    data = {"project": project_info["name"], "sink": sink}
    if json_mode:
        emit_json_payload("event disable", ok=True, data=data)
        return
    echo(f"[green][OK] 已停用事件 sink：{safe(sink['name'])}[/green]")


@event_group.command("schema")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def schema_cmd(ctx: click.Context, json_mode: bool) -> None:
    """列出 CodePilot 事件 schema catalog。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    data = {"schema_version": event_plugins.SCHEMA_VERSION, "schemas": event_plugins.event_schemas()}
    if json_mode:
        emit_json_payload("event schema", ok=True, data=data)
        return
    for schema in data["schemas"]:
        click.echo(f"{schema['type']}\t{schema['description']}")


@event_group.command("test")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--event", "event_type", default="test.event", show_default=True, help="测试事件类型")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def test_cmd(ctx: click.Context, project: str | None, event_type: str, json_mode: bool) -> None:
    """向已启用事件 sink 发送一条测试事件。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        event = event_plugins.build_test_event(project_info["name"], event_type)
        results = event_plugins.dispatch_event_to_sinks(project_info["path"], event)
    except (event_plugins.EventPluginError, click.ClickException) as exc:
        _emit_error(ctx, "event test", json_mode, exc)
        return

    delivered = len([item for item in results if item["status"] == "delivered"])
    data = {"project": project_info["name"], "event": event, "delivered": delivered, "results": results}
    if json_mode:
        emit_json_payload("event test", ok=True, data=data)
        return
    echo(f"[green]已投递 {delivered} 个事件 sink[/green]")
