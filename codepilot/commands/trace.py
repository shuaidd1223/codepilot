"""Read-only project activity trace command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from rich import box
from rich.table import Table

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.commands.status import _short_text
from codepilot.core.output import echo, terminal_console
from codepilot.core.workflow_state import workflow_dirs
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


def _event(
    *,
    timestamp: str,
    source: str,
    event: str,
    project: str,
    message: str,
    task_id: int | None = None,
    status: str = "",
    phase: str = "",
    detail: str = "",
) -> dict[str, Any]:
    return {
        "timestamp": str(timestamp or ""),
        "source": source,
        "event": event,
        "project": project,
        "task_id": task_id,
        "status": status,
        "phase": phase,
        "message": message,
        "detail": detail,
    }


def _workflow_states(project_path: str | Path) -> list[dict[str, Any]]:
    state_dir = workflow_dirs(project_path)["state"]
    if not state_dir.exists():
        return []
    states = []
    for path in sorted(state_dir.glob("*.json")):
        if path.name == "active-workflow.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["_path"] = path.name
            states.append(payload)
    return states


def _filter_tasks(tasks: list[dict[str, Any]], task_id: int | None) -> list[dict[str, Any]]:
    if task_id is None:
        return tasks
    return [task for task in tasks if int(task["id"]) == int(task_id)]


def _task_lifecycle_events(project: str, task: dict[str, Any]) -> list[dict[str, Any]]:
    tid = int(task["id"])
    title = str(task.get("title") or "")
    status = str(task.get("status") or "")
    phase = str(task.get("run_phase") or "")
    events: list[dict[str, Any]] = []

    if task.get("created_at"):
        events.append(
            _event(
                timestamp=task["created_at"],
                source="task",
                event="task.created",
                project=project,
                task_id=tid,
                status=status,
                message=f"#{tid} 创建任务：{title}",
            )
        )
    if task.get("started_at"):
        events.append(
            _event(
                timestamp=task["started_at"],
                source="task",
                event="task.started",
                project=project,
                task_id=tid,
                status=status,
                phase=phase,
                message=f"#{tid} 开始执行：{title}",
            )
        )
    if task.get("heartbeat_at"):
        events.append(
            _event(
                timestamp=task["heartbeat_at"],
                source="task",
                event="task.heartbeat",
                project=project,
                task_id=tid,
                status=status,
                phase=phase,
                message=f"#{tid} 心跳：{title}",
                detail=str(task.get("last_output") or ""),
            )
        )
    if task.get("completed_at"):
        events.append(
            _event(
                timestamp=task["completed_at"],
                source="task",
                event="task.completed",
                project=project,
                task_id=tid,
                status=status,
                message=f"#{tid} 结束：{title}",
                detail=str(task.get("error_message") or task.get("delivery_record") or ""),
            )
        )

    return events


def _task_log_events(project: str, task_id: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for log in db.list_task_logs(task_id):
        phase = str(log.get("phase") or "")
        if log.get("started_at"):
            events.append(
                _event(
                    timestamp=log["started_at"],
                    source="task_log",
                    event="task_log.started",
                    project=project,
                    task_id=task_id,
                    phase=phase,
                    message=f"#{task_id} 阶段开始：{phase}",
                    detail=str(log.get("output") or ""),
                )
            )
        if log.get("finished_at"):
            exit_code = log.get("exit_code")
            events.append(
                _event(
                    timestamp=log["finished_at"],
                    source="task_log",
                    event="task_log.finished",
                    project=project,
                    task_id=task_id,
                    phase=phase,
                    status=f"exit={exit_code}" if exit_code is not None else "",
                    message=f"#{task_id} 阶段结束：{phase}",
                    detail=str(log.get("output") or ""),
                )
            )
    return events


def _collect_task_events(project: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for task in tasks:
        tid = int(task["id"])
        events.extend(_task_lifecycle_events(project, task))
        events.extend(_task_log_events(project, tid))
    return events


def _service_events(project: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for state in db.list_service_states():
        scope = str(state.get("scope") or "").strip()
        if scope not in {"", project}:
            continue
        service = str(state.get("service") or "")
        label = f"{service}:{scope}" if scope else service
        timestamp = str(state.get("heartbeat_at") or state.get("updated_at") or "")
        if not timestamp:
            continue
        events.append(
            _event(
                timestamp=timestamp,
                source="service",
                event="service.heartbeat",
                project=project,
                status=str(state.get("status") or ""),
                message=f"服务心跳：{label}",
                detail=str(state.get("log_path") or ""),
            )
        )
    return events


def _workflow_event(project: str, state: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = str(state.get("updated_at") or state.get("started_at") or state.get("completed_at") or "")
    if not timestamp:
        return None
    mode = str(state.get("mode") or state.get("_path") or "")
    active = state.get("active")
    return _event(
        timestamp=timestamp,
        source="workflow",
        event="workflow.updated",
        project=project,
        status="active" if active is True else "inactive" if active is False else "",
        phase=str(state.get("current_phase") or ""),
        message=f"workflow 更新：{mode}",
        detail=str(state.get("context_path") or state.get("_path") or ""),
    )


def _workflow_events(project: str, project_path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for state in _workflow_states(project_path):
        event = _workflow_event(project, state)
        if event is not None:
            events.append(event)
    return events


def _memory_events(project_info: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from codepilot.core.memory import read_memory_events
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for item in read_memory_events(project_info, limit=0):
        events.append(
            _event(
                timestamp=str(item.get("timestamp") or ""),
                source="memory",
                event=str(item.get("event_type") or "memory.event"),
                project=str(item.get("project") or project_info["name"]),
                status=str(item.get("confidence") or ""),
                phase="",
                message=str(item.get("summary") or ""),
                detail=json.dumps(item.get("details") or {}, ensure_ascii=False, sort_keys=True),
            )
        )
    return events


def _sort_and_limit_events(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    sorted_events = sorted(events, key=lambda item: item["timestamp"], reverse=True)
    if limit > 0:
        return sorted_events[:limit]
    return sorted_events


def collect_trace_events(
    project_info: dict,
    *,
    task_id: int | None = None,
    limit: int = 30,
    include_services: bool = True,
    include_workflow: bool = True,
    include_memory: bool = True,
) -> list[dict[str, Any]]:
    """Collect a merged activity timeline from local CodePilot state."""
    project = project_info["name"]
    project_path = project_info["path"]
    tasks = _filter_tasks(db.list_tasks(project=project), task_id)
    events = _collect_task_events(project, tasks)

    if task_id is None:
        if include_services:
            events.extend(_service_events(project))
        if include_workflow:
            events.extend(_workflow_events(project, project_path))
        if include_memory:
            events.extend(_memory_events(project_info))

    return _sort_and_limit_events(events, limit)


def render_trace(events: list[dict[str, Any]]) -> None:
    console = terminal_console()
    table = Table(show_header=True, header_style="bold bright_black", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("时间", width=19, no_wrap=True)
    table.add_column("来源", width=10, no_wrap=True)
    table.add_column("事件", width=18, no_wrap=True)
    table.add_column("状态", width=10, no_wrap=True)
    table.add_column("阶段", width=12, no_wrap=True, overflow="ellipsis")
    table.add_column("信息", ratio=3, no_wrap=True, overflow="ellipsis")
    table.add_column("详情", ratio=2, no_wrap=True, overflow="ellipsis")
    for item in events:
        table.add_row(
            str(item["timestamp"])[:19],
            item["source"],
            item["event"],
            item.get("status") or "-",
            item.get("phase") or "-",
            _short_text(item.get("message") or "", 90),
            _short_text(item.get("detail") or "", 90),
        )
    console.print()
    console.print(table)
    console.print()


@click.command(context_settings={"allow_interspersed_args": False})
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--task", "task_id", type=int, help="只显示指定任务的时间线")
@click.option("--limit", type=int, default=30, show_default=True, help="最多显示事件数；0 表示不限制")
@click.option("--no-services", is_flag=True, help="不包含后台服务心跳")
@click.option("--no-workflow", is_flag=True, help="不包含 workflow state")
@click.option("--no-memory", is_flag=True, help="不包含 memory event log")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def trace(
    ctx: click.Context,
    project: str | None,
    task_id: int | None,
    limit: int,
    no_services: bool,
    no_workflow: bool,
    no_memory: bool,
    json_mode: bool,
) -> None:
    """显示项目最近活动时间线，用于排障任务、日志和服务状态."""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    events = collect_trace_events(
        project_info,
        task_id=task_id,
        limit=max(limit, 0),
        include_services=not no_services,
        include_workflow=not no_workflow,
        include_memory=not no_memory,
    )
    data = {
        "project": project_info["name"],
        "project_path": project_info["path"],
        "task_id": task_id,
        "count": len(events),
        "events": events,
    }
    if json_mode:
        emit_json_payload("trace", ok=True, data=data)
        return
    if not events:
        echo("[yellow]暂无 trace 事件[/yellow]")
        return
    render_trace(events)
