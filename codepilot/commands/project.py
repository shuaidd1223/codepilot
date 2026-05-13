"""Project management commands."""

from __future__ import annotations

import json

import click

from codepilot.storage import database as db
from codepilot.core.output import echo


@click.group("project")
def project_group() -> None:
    """管理已注册项目。"""


@click.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def delete_project(ctx: click.Context, name: str, yes: bool, json_mode: bool) -> None:
    """删除项目注册记录和关联任务/会话，不删除工作目录。"""
    db.init_db()
    root_obj = ctx.find_root().obj or {}
    if not json_mode:
        json_mode = root_obj.get("json_mode", False)

    project = db.get_project(name)
    if not project:
        raise click.ClickException(f"项目 '{name}' 未注册。")
    project_name = str(project["name"])

    stats = db.get_task_stats(project_name)
    if json_mode:
        ok = db.delete_project(project_name)
        click.echo(json.dumps({"ok": ok, "project": project_name, "path": project["path"]}, ensure_ascii=False, indent=2))
        return

    if not yes:
        click.echo(f"将删除项目注册记录：{project_name}")
        click.echo(f"  路径: {project['path']}")
        click.echo(
            f"  关联任务: {stats['total']} 总 / {stats['in_progress']} 进行 / "
            f"{stats['backlog']} 待办 / {stats['done']} 完成"
        )
        click.echo("  工作目录不会被删除。")
        click.confirm("确认删除？", abort=True)

    db.delete_project(project_name)
    echo(f"[green][OK] 项目 '{project_name}' 已删除[/green]")
    click.echo(f"  工作目录保留: {project['path']}")


project_group.add_command(delete_project)
project_group.add_command(delete_project, "rm")

