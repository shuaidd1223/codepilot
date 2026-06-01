"""codepilot hud 命令：轻量工作流状态栏视图."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.commands.status import _short_text
from codepilot.core.output import echo, terminal_console
from codepilot.storage import database as db
from codepilot.webapp.display_sort import sort_tasks_for_display


STATUS_LABELS = {
    "backlog": "待办",
    "in_progress": "进行中",
    "done": "完成",
    "failed": "失败",
    "cancelled": "取消",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _task_activity_at(task: dict) -> str:
    return str(
        task.get("heartbeat_at")
        or task.get("completed_at")
        or task.get("started_at")
        or task.get("created_at")
        or ""
    )


def _recent_tasks(tasks: list[dict], *, limit: int = 3) -> list[dict[str, Any]]:
    ordered = sorted(tasks, key=_task_activity_at, reverse=True)
    recent = []
    for task in ordered[:limit]:
        recent.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "status_label": STATUS_LABELS.get(task["status"], task["status"]),
                "priority": task["priority"],
                "agent": task["agent"],
                "phase": task.get("run_phase") or "",
                "activity_at": _task_activity_at(task),
                "summary": (
                    task.get("error_message")
                    or task.get("last_output")
                    or task.get("delivery_record")
                    or ""
                )[:160],
            }
        )
    return recent


def _project_mode(stats: dict) -> str:
    if stats["failed"] or stats["cancelled"]:
        return "attention"
    if stats["in_progress"]:
        return "running"
    if stats["backlog"]:
        return "queued"
    return "idle"


def _service_label(state: dict) -> str:
    scope = str(state.get("scope") or "").strip()
    suffix = f":{scope}" if scope else ""
    return f"{state.get('service')}{suffix}"


def _filter_services(project: str | None) -> list[dict]:
    services = db.list_service_states()
    if not project:
        return services
    return [
        state
        for state in services
        if str(state.get("scope") or "").strip() in {"", project}
    ]


def _check_blocked_reason_for_project(tasks: list[dict]) -> str | None:
    from codepilot.commands.run_builtin_core import _preflight_blocked_detail
    for task in tasks:
        if task.get("status") == "backlog":
            detail = _preflight_blocked_detail(task.get("error_message") or "")
            if detail:
                return detail["reason"]
    return None


def collect_hud_snapshot(
    *,
    project: str | None = None,
    all_projects: bool = False,
    recent_limit: int = 3,
    preset: str = "focused",
) -> dict[str, Any]:
    """Collect a compact read-only snapshot for the HUD command."""
    projects = db.list_projects()
    selected_project = project
    if not selected_project and not all_projects:
        current = db.find_project_by_path(Path.cwd())
        selected_project = current["name"] if current else None

    if selected_project:
        projects = [proj for proj in projects if proj["name"] == selected_project]

    project_rows = []
    totals = {
        "backlog": 0,
        "in_progress": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "total": 0,
    }
    for proj in projects:
        stats = db.get_task_stats(proj["name"])
        tasks = sort_tasks_for_display(db.list_tasks(project=proj["name"]))
        for key in totals:
            totals[key] += int(stats.get(key) or 0)
        blocked_reason = _check_blocked_reason_for_project(tasks)
        project_rows.append(
            {
                "name": proj["name"],
                "path": proj["path"],
                "default_mode": proj.get("default_mode") or "",
                "stats": stats,
                "mode": _project_mode(stats),
                "active_tasks": [
                    task
                    for task in _recent_tasks(
                        [task for task in tasks if task["status"] == "in_progress"],
                        limit=recent_limit,
                    )
                ],
                "recent_tasks": _recent_tasks(tasks, limit=recent_limit),
                "blocked_reason": blocked_reason,
            }
        )

    services = [
        {
            "service": state.get("service"),
            "scope": state.get("scope") or "",
            "label": _service_label(state),
            "status": state.get("status") or "",
            "pid": state.get("pid"),
            "heartbeat_at": state.get("heartbeat_at") or "",
            "updated_at": state.get("updated_at") or "",
            "log_path": state.get("log_path") or "",
            "meta": state.get("meta") or {},
        }
        for state in _filter_services(selected_project if not all_projects else None)
    ]

    return {
        "generated_at": _now_iso(),
        "preset": preset,
        "project": selected_project,
        "projects": project_rows,
        "project_count": len(project_rows),
        "totals": totals,
        "services": services,
    }


def _stat_text(stats: dict) -> str:
    return (
        f"[blue]进行中:{stats['in_progress']}[/blue]  "
        f"[yellow]待办:{stats['backlog']}[/yellow]  "
        f"[red]失败:{stats['failed']}[/red]  "
        f"[magenta]取消:{stats['cancelled']}[/magenta]  "
        f"[green]完成:{stats['done']}[/green]  "
        f"[cyan]总计:{stats['total']}[/cyan]"
    )


def _mode_style(mode: str) -> str:
    return {
        "attention": "bold red",
        "running": "bold blue",
        "queued": "bold yellow",
        "idle": "dim",
    }.get(mode, "white")


def _project_table(projects: list[dict]) -> Table:
    table = Table(show_header=True, header_style="bold bright_black", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("项目", style="bold", no_wrap=True)
    table.add_column("模式", width=10, no_wrap=True)
    table.add_column("运行", justify="right", width=5, no_wrap=True)
    table.add_column("待办", justify="right", width=5, no_wrap=True)
    table.add_column("失败", justify="right", width=5, no_wrap=True)
    table.add_column("完成", justify="right", width=5, no_wrap=True)
    table.add_column("最近活动", ratio=3, no_wrap=True, overflow="ellipsis")

    for proj in projects:
        stats = proj["stats"]
        recent = proj["recent_tasks"][0] if proj["recent_tasks"] else None
        recent_text = "-"
        if recent:
            recent_text = f"#{recent['id']} {recent['status_label']} · {recent['title']}"
        mode_style = _mode_style(proj["mode"])
        table.add_row(
            proj["name"],
            f"[{mode_style}]{proj['mode']}[/]",
            str(stats["in_progress"]),
            str(stats["backlog"]),
            str(stats["failed"] + stats["cancelled"]),
            str(stats["done"]),
            _short_text(recent_text, 90),
        )
    return table


def _active_task_table(project: dict) -> Table:
    table = Table(show_header=True, header_style="bold bright_black", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("ID", justify="right", width=5, no_wrap=True)
    table.add_column("P", justify="center", width=3, no_wrap=True)
    table.add_column("Agent", width=8, no_wrap=True, overflow="ellipsis")
    table.add_column("阶段", width=12, no_wrap=True, overflow="ellipsis")
    table.add_column("任务", ratio=3, no_wrap=True, overflow="ellipsis")
    table.add_column("最近信息", ratio=3, no_wrap=True, overflow="ellipsis")
    for task in project["active_tasks"]:
        table.add_row(
            str(task["id"]),
            task["priority"],
            task["agent"],
            task["phase"] or "-",
            _short_text(task["title"], 72),
            _short_text(task["summary"] or task["activity_at"], 72),
        )
    return table


def _service_table(services: list[dict]) -> Table:
    table = Table(show_header=True, header_style="bold bright_black", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("服务", style="bold", no_wrap=True)
    table.add_column("状态", width=10, no_wrap=True)
    table.add_column("PID", justify="right", width=8, no_wrap=True)
    table.add_column("心跳", width=19, no_wrap=True)
    table.add_column("日志", ratio=2, no_wrap=True, overflow="ellipsis")
    for state in services:
        status = str(state.get("status") or "")
        style = "green" if status == "running" else "yellow" if status else "dim"
        table.add_row(
            state["label"],
            f"[{style}]{status or '-'}[/{style}]",
            str(state.get("pid") or "-"),
            str(state.get("heartbeat_at") or "-")[:19],
            _short_text(state.get("log_path") or "", 72),
        )
    return table


def render_hud(snapshot: dict[str, Any], *, preset: str = "focused", console: Console | None = None) -> None:
    """Render a compact HUD snapshot."""
    console = console or terminal_console()
    totals = snapshot["totals"]
    title = "CodePilot HUD"
    if snapshot.get("project"):
        title += f" · {snapshot['project']}"

    console.print()
    console.print(Panel(Text(title, style="bold cyan"), border_style="cyan", padding=(0, 1)))
    console.print(f"[dim]{snapshot['generated_at']}[/dim]  {_stat_text(totals)}")
    console.print()

    if not snapshot["projects"]:
        console.print("[yellow]没有可显示的项目[/yellow]")
        return

    if preset == "minimal":
        console.print(_project_table(snapshot["projects"]))
        console.print()
        return

    renderables: list[Any] = [_project_table(snapshot["projects"])]
    for proj in snapshot["projects"]:
        if proj.get("blocked_reason"):
            renderables.append(
                Panel(
                    Text(f"  {proj['blocked_reason']}", style="yellow"),
                    title=f"[yellow]⛔ {proj['name']} 阻塞 — 需先处理工作区[/yellow]",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
    for proj in snapshot["projects"]:
        if proj["active_tasks"]:
            renderables.append(
                Panel(
                    _active_task_table(proj),
                    title=f"[bold blue]{proj['name']} 运行中[/bold blue]",
                    border_style="blue",
                    padding=(0, 1),
                )
            )
    if snapshot["services"] and preset == "full":
        renderables.append(
            Panel(
                _service_table(snapshot["services"]),
                title="[bold green]服务状态[/bold green]",
                border_style="green",
                padding=(0, 1),
            )
        )
    console.print(Group(*renderables))
    console.print()


def _resolve_project_or_abort(project: str | None) -> str | None:
    if not project:
        return None
    if not db.get_project(project):
        raise click.ClickException(f"项目 '{project}' 未注册")
    return project


@click.command(context_settings={"allow_interspersed_args": False})
@click.option("--project", "-p", help="项目名称；不指定时优先使用当前目录所属项目")
@click.option("--all", "all_projects", is_flag=True, help="显示所有项目，忽略当前目录自动识别")
@click.option(
    "--preset",
    type=click.Choice(["minimal", "focused", "full"], case_sensitive=False),
    default="focused",
    show_default=True,
    help="显示密度",
)
@click.option("--watch", is_flag=True, help="持续刷新 HUD")
@click.option("--interval", type=float, default=1.0, show_default=True, help="watch 刷新间隔秒数")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def hud(
    ctx: click.Context,
    project: str | None,
    all_projects: bool,
    preset: str,
    watch: bool,
    interval: float,
    json_mode: bool,
):
    """显示轻量工作流 HUD：项目、队列、运行任务、服务状态."""
    db.init_db()
    json_mode = resolve_json_mode(ctx, json_mode)
    project = _resolve_project_or_abort(project)
    preset = preset.lower()

    if json_mode:
        if watch:
            raise click.ClickException("--json 不支持 --watch")
        snapshot = collect_hud_snapshot(project=project, all_projects=all_projects, preset=preset)
        emit_json_payload("hud", ok=True, data=snapshot)
        return

    console = terminal_console()
    if watch:
        try:
            while True:
                try:
                    snapshot = collect_hud_snapshot(project=project, all_projects=all_projects, preset=preset)
                    console.clear()
                    render_hud(snapshot, preset=preset, console=console)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    echo(f"[red]HUD 刷新异常，跳过本轮[{_now_iso()}][/red]")
                time.sleep(max(interval, 0.2))
        except KeyboardInterrupt:
            return

    snapshot = collect_hud_snapshot(project=project, all_projects=all_projects, preset=preset)
    if not snapshot["projects"] and project:
        echo(f"[yellow]项目 '{project}' 暂无 HUD 数据[/yellow]")
        return
    render_hud(snapshot, preset=preset, console=console)
