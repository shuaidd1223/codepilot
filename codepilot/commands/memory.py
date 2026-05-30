"""Memory event log commands."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.memory import MemoryError, memory_events_path, read_memory_events
from codepilot.core.output import echo
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


def _emit_or_raise(ctx: click.Context, command: str, json_mode: bool, exc: Exception) -> None:
    if json_mode:
        emit_json_payload(command, ok=False, data={}, error=str(exc), error_code="memory_error")
        ctx.exit(1)
    raise click.ClickException(str(exc))


@click.group("memory")
def memory_group() -> None:
    """查看项目本地自动记忆事件。"""


@memory_group.command("events")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--limit", type=int, default=50, show_default=True, help="最多显示事件数；0 表示不限制")
@click.option("--type", "event_type", help="只显示指定 event_type")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def events_cmd(ctx: click.Context, project: str | None, limit: int, event_type: str | None, json_mode: bool) -> None:
    """列出自动观察事实事件。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        events = read_memory_events(project_info, limit=max(limit, 0), event_type=event_type)
        path = memory_events_path(project_info)
    except (MemoryError, click.ClickException) as exc:
        _emit_or_raise(ctx, "memory events", json_mode, exc)
        return
    data = {
        "project": project_info["name"],
        "project_path": project_info["path"],
        "path": str(path),
        "count": len(events),
        "events": events,
    }
    if json_mode:
        emit_json_payload("memory events", ok=True, data=data)
        return
    if not events:
        echo("[yellow]暂无 memory events[/yellow]")
        return
    for item in events:
        click.echo(f"{str(item.get('timestamp') or '')[:19]}\t{item.get('event_type')}\t{item.get('summary')}")
