"""codepilot status 命令：看板视图."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from codepilot import db
from codepilot.output import echo


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
    # 优先用本地 --json，否则用全局
    if not json_mode and ctx.parent:
        json_mode = ctx.parent.obj.get("json_mode", False)

    if project:
        return _show_project_status(project, verbose, json_mode)
    else:
        return _show_all_projects_status(verbose, json_mode)


def _show_project_status(project: str, verbose: bool, json_mode: bool):
    """显示单个项目的看板。"""
    proj = db.get_project(project)
    if not proj:
        echo(f"[red]错误：项目 '{project}' 未注册[/red]")
        click.echo("  运行 codepilot init 先注册项目")
        return

    if json_mode:
        tasks = db.list_tasks(project=project)
        stats = db.get_task_stats(project)
        click.echo(json.dumps({
            "project": project,
            "stats": stats,
            "tasks": tasks,
        }, ensure_ascii=False, indent=2))
        return

    console = Console()

    console.print(f"\n[bold cyan]CodePilot[/bold cyan]  {project}  ({proj['path']})\n")

    stats = db.get_task_stats(project)
    console.print(
        f"  backlog: [yellow]{stats['backlog']}[/yellow]  "
        f"in-progress: [blue]{stats['in_progress']}[/blue]  "
        f"done: [green]{stats['done']}[/green]  "
        f"failed: [red]{stats['failed']}[/red]  "
        f"total: {stats['total']}\n"
    )

    status_labels = {
        "backlog": ("待办", "yellow"),
        "in_progress": ("进行中", "blue"),
        "done": ("已完成", "green"),
        "failed": ("失败", "red"),
    }

    for status_key, (label, color) in status_labels.items():
        tasks = db.list_tasks(project=project, status=status_key)
        if not tasks:
            continue

        console.print(f"[bold {color}]{label} ({len(tasks)})[/bold {color}]")
        table = Table(show_header=True, header_style="bold dim", box=None)
        table.add_column("ID", style="dim", width=4)
        table.add_column("标题", style="white")
        table.add_column("优先级", width=5)
        table.add_column("Agent", width=6)
        if verbose:
            table.add_column("创建时间", style="dim")
            table.add_column("分支", style="dim")

        for t in tasks:
            title = (t["title"][:60] + "...") if len(t["title"]) > 60 else t["title"]
            row = [
                str(t["id"]),
                title,
                t["priority"],
                t["agent"],
            ]
            if verbose:
                row.append(t.get("created_at", "")[:19] if t.get("created_at") else "")
                row.append(t.get("branch_name") or "-")
            table.add_row(*row)

        console.print(table)
        console.print()


def _show_all_projects_status(verbose: bool, json_mode: bool):
    """显示所有项目的汇总看板。"""
    projects = db.list_projects()
    if not projects:
        echo("[yellow]没有已注册的项目[/yellow]")
        return

    if json_mode:
        all_data = []
        for proj in projects:
            stats = db.get_task_stats(proj["name"])
            tasks = db.list_tasks(project=proj["name"])
            all_data.append({
                "project": proj["name"],
                "path": proj["path"],
                "stats": stats,
                "tasks": tasks,
            })
        click.echo(json.dumps(all_data, ensure_ascii=False, indent=2))
        return

    console = Console()
    console.print("\n[bold cyan]CodePilot[/bold cyan]  所有项目\n")

    for proj in projects:
        stats = db.get_task_stats(proj["name"])
        if stats["total"] == 0:
            continue
        console.print(
            f"[bold]{proj['name']}[/bold]  ({proj['path']})  "
            f"backlog: [yellow]{stats['backlog']}[/yellow]  "
            f"in-progress: [blue]{stats['in_progress']}[/blue]  "
            f"done: [green]{stats['done']}[/green]  "
            f"failed: [red]{stats['failed']}[/red]"
        )
    console.print()
