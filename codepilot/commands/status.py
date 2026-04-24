"""codepilot status 命令：看板视图."""

from __future__ import annotations

import click
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markup import escape as _markup_escape
from rich.text import Text
from rich.console import Group

from codepilot import db
from codepilot.display_sort import sort_tasks_for_display
from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.output import echo
from codepilot.runtime import runtime_summary

STATUS_META = {
    "backlog": ("待办", "yellow"),
    "in_progress": ("进行中", "blue"),
    "done": ("已完成", "green"),
    "failed": ("失败", "red"),
    "cancelled": ("已取消", "magenta"),
}


def _short_text(value: str | None, max_len: int = 80) -> str:
    """Render untrusted text safely for a rich Table cell (escapes markup)."""
    text = (value or "").replace("\n", " ").strip()
    if not text:
        return "-"
    truncated = (text[: max_len - 3] + "...") if len(text) > max_len else text
    return _markup_escape(truncated)


def _status_badge(status: str) -> str:
    label, color = STATUS_META.get(status, (status, "white"))
    return f"[bold {color}]{label}[/{color}]"


def _status_stats_line(stats: dict, *, include_cancelled: bool = False) -> str:
    parts = [
        f"[blue]进行中:{stats['in_progress']}[/blue]",
        f"[yellow]待办:{stats['backlog']}[/yellow]",
        f"[red]失败:{stats['failed']}[/red]",
    ]
    if include_cancelled:
        parts.append(f"[magenta]已取消:{stats['cancelled']}[/magenta]")
    parts.extend(
        [
            f"[green]完成:{stats['done']}[/green]",
            f"[cyan]总计:{stats['total']}[/cyan]",
        ]
    )
    return "  ".join(parts)


def _build_console(console: Console | None = None) -> Console:
    if console is not None:
        return console
    import shutil

    term_width = shutil.get_terminal_size((120, 24)).columns
    return Console(width=min(term_width, 140))


def _metric_panel(label: str, value: int, color: str) -> Panel:
    body = Text()
    body.append(f"{value}\n", style=f"bold {color}")
    body.append(label, style="dim")
    return Panel.fit(body, border_style=color, padding=(0, 2))


def _task_recent(task: dict, *, verbose: bool = False) -> str:
    if task["status"] == "in_progress":
        return runtime_summary(task)
    return _short_text(
        task.get("error_message") or task.get("last_output") or task.get("delivery_record"),
        110 if verbose else 72,
    )


def _task_table(tasks: list[dict], *, verbose: bool = False) -> Table:
    table = Table(show_header=True, header_style="bold bright_black", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("ID", style="dim", width=4, justify="right", no_wrap=True)
    table.add_column("P", width=3, justify="center", no_wrap=True)
    table.add_column("Agent", width=8, no_wrap=True, overflow="ellipsis")
    table.add_column("标题", min_width=24, ratio=3, no_wrap=True, overflow="ellipsis")
    table.add_column("阶段", width=10, no_wrap=True, overflow="ellipsis")
    table.add_column("最近信息", min_width=30, ratio=4, no_wrap=True, overflow="ellipsis")
    if verbose:
        table.add_column("创建时间", style="dim", width=19, no_wrap=True)
        table.add_column("最后输出", style="dim", min_width=24, ratio=3, no_wrap=True, overflow="ellipsis")

    for task in tasks:
        row = [
            str(task["id"]),
            task["priority"],
            task["agent"],
            _short_text(task["title"], 68),
            task.get("run_phase") or "-",
            _task_recent(task, verbose=verbose),
        ]
        if verbose:
            row.append((task.get("created_at") or "")[:19] or "-")
            row.append(_short_text(task.get("last_output") or task.get("error_message") or task.get("delivery_record"), 84))
        table.add_row(*row)
    return table


def _section_panel(title: str, color: str, tasks: list[dict], *, verbose: bool = False) -> Panel:
    body = Group(_task_table(tasks, verbose=verbose))
    return Panel(body, title=f"[bold {color}]{title}[/bold {color}]", border_style=color, padding=(0, 1))


def render_project_dashboard(
    project: str,
    *,
    verbose: bool = False,
    include_done: bool = True,
    max_rows: int = 12,
    title: str | None = None,
    console: Console | None = None,
) -> None:
    """Render one project dashboard as a compact task table."""
    proj = db.get_project(project)
    if not proj:
        echo(f"[red]错误：项目 '{project}' 未注册[/red]")
        return

    console = _build_console(console)
    stats = db.get_task_stats(project)
    header = title or f"CodePilot  {project}"

    # 紧凑统计行（替代 6 个 Panel 方块）
    stat_line = _status_stats_line(stats)
    console.print()
    console.print(f"[bold cyan]{header}[/bold cyan]  {stat_line}")
    console.print(f"[dim]{proj['path']}[/dim]")
    console.print()

    tasks = db.list_tasks(project=project)
    if not include_done:
        tasks = [task for task in tasks if task["status"] != "done"]
    if not tasks:
        console.print("[dim]当前没有可显示的任务[/dim]\n")
        return

    tasks = sort_tasks_for_display(tasks)
    sections = [
        ("进行中", "blue", [task for task in tasks if task["status"] == "in_progress"][: max(1, max_rows // 2)]),
        ("待办", "yellow", [task for task in tasks if task["status"] == "backlog"][: max_rows]),
        ("失败 / 取消", "red", [task for task in tasks if task["status"] in {"failed", "cancelled"}][: max_rows // 2 or 1]),
    ]
    if include_done:
        sections.append(("最近完成", "green", [task for task in tasks if task["status"] == "done"][: max_rows // 2 or 1]))

    for section_title, color, section_tasks in sections:
        if section_tasks:
            console.print(_section_panel(section_title, color, section_tasks, verbose=verbose))
            console.print()


def render_project_stats(
    project: str,
    *,
    title: str | None = None,
    console: Console | None = None,
) -> None:
    """Render one project's aggregate task stats without the dashboard detail."""
    proj = db.get_project(project)
    if not proj:
        echo(f"[red]错误：项目 '{project}' 未注册[/red]")
        return

    stats = db.get_task_stats(project)
    console = _build_console(console)
    header = title or f"状态统计  {project}"

    console.print()
    console.print(f"[bold cyan]{header}[/bold cyan]  {_status_stats_line(stats, include_cancelled=True)}")
    console.print(f"[dim]{proj['path']}[/dim]")
    console.print()


def _resolve_project(ctx: click.Context, param: str, value: str | None) -> str | None:
    """解析项目名：支持空值（查看全部）。"""
    if not value:
        return None
    db.init_db()
    proj = db.get_project(value)
    if not proj:
        echo(f"[red]错误: 项目 '{value}' 未注册[/red]")
        raise click.Abort()
    return value


@click.command(context_settings={"allow_interspersed_args": False})
@click.option(
    "--project", "-p",
    callback=_resolve_project,
    help="项目名称（不指定则显示所有项目）",
)
@click.option("--verbose", "-v", is_flag=True, help="显示详细信息")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def status(ctx: click.Context, project: str | None, verbose: bool, json_mode: bool):
    """
    查看任务看板：backlog / in-progress / done / failed 四列状态.
    """
    db.init_db()
    json_mode = resolve_json_mode(ctx, json_mode)

    if project:
        return _show_project_status(project, verbose, json_mode)
    else:
        return _show_all_projects_status(verbose, json_mode)


def _show_project_status(project: str, verbose: bool, json_mode: bool):
    """显示单个项目的看板。"""
    proj = db.get_project(project)
    if not proj:
        if json_mode:
            emit_json_payload(
                "status",
                ok=False,
                data={"project": project, "stats": {}, "tasks": []},
                error=f"项目 '{project}' 未注册",
                error_code="project_not_registered",
            )
        else:
            echo(f"[red]错误：项目 '{project}' 未注册[/red]")
            click.echo("  运行 codepilot init 先注册项目")
        return

    if json_mode:
        tasks = sort_tasks_for_display(db.list_tasks(project=project))
        stats = db.get_task_stats(project)
        emit_json_payload(
            "status",
            ok=True,
            data={
                "project": project,
                "stats": stats,
                "tasks": tasks,
            },
        )
        return

    render_project_dashboard(project, verbose=verbose, include_done=True, title=f"CodePilot  {project}")


def _show_all_projects_status(verbose: bool, json_mode: bool):
    """显示所有项目的汇总看板。"""
    projects = db.list_projects()
    if not projects:
        if json_mode:
            emit_json_payload("status", ok=True, data={"projects": [], "count": 0})
        else:
            echo("[yellow]没有已注册的项目[/yellow]")
        return

    if json_mode:
        all_data = []
        for proj in projects:
            stats = db.get_task_stats(proj["name"])
            tasks = sort_tasks_for_display(db.list_tasks(project=proj["name"]))
            all_data.append({
                "project": proj["name"],
                "path": proj["path"],
                "stats": stats,
                "tasks": tasks,
            })
        emit_json_payload("status", ok=True, data={"projects": all_data, "count": len(all_data)})
        return

    console = Console(width=160)
    console.print()
    console.print(Panel(Text("CodePilot 所有项目", style="bold cyan"), border_style="cyan", padding=(0, 1)))

    table = Table(show_header=True, header_style="bold bright_black", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("项目", style="bold")
    table.add_column("路径", style="dim", ratio=3)
    table.add_column("进行中", justify="right", width=7)
    table.add_column("待办", justify="right", width=6)
    table.add_column("失败", justify="right", width=6)
    table.add_column("已完成", justify="right", width=7)
    table.add_column("状态摘要", ratio=2)

    for proj in projects:
        stats = db.get_task_stats(proj["name"])
        if stats["total"] == 0:
            continue
        live = db.list_tasks(project=proj["name"], status="in_progress")
        live_text = _short_text(runtime_summary(live[0]) if live else "暂无运行中任务", 60)
        table.add_row(
            proj["name"],
            proj["path"],
            str(stats["in_progress"]),
            str(stats["backlog"]),
            str(stats["failed"] + stats["cancelled"]),
            str(stats["done"]),
            live_text,
        )

    console.print(table)
    console.print()
