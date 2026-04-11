"""codepilot 任务管理命令：edit / rm / done / find."""

from __future__ import annotations

import json

import click

from codepilot import db
from codepilot.commands.status import _resolve_project


# ── done ──────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_id", type=int)
@click.option("--message", "-m", default="", help="完成备注/交付说明")
def done(task_id: int, message: str):
    """手动标记任务为完成（不运行 Agent）。"""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        click.echo(f"[red]任务 #{task_id} 不存在[/red]")
        return

    from datetime import datetime
    updates = {"status": "done", "completed_at": datetime.now().isoformat()}
    if message:
        updates["delivery_record"] = message
    db.update_task(task_id, **updates)
    click.echo(f"[green][OK] 任务 #{task_id} 已标记为 done[/green]  {task['title']}")


# ── edit ───────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_id", type=int)
@click.option("--title", "-t", help="修改标题")
@click.option("--priority", "-p",
               type=click.Choice(["P0", "P1", "P2", "P3"], case_sensitive=False),
               help="修改优先级")
@click.option("--status", "-s",
               type=click.Choice(["backlog", "in_progress", "done", "failed"], case_sensitive=False),
               help="修改状态")
@click.option("--agent", "-a",
               type=click.Choice(["dual", "builder", "reviewer", "claude", "codex"], case_sensitive=False),
               help="修改 Agent 模式")
@click.option("--depends", "-d", help="修改依赖，格式: 1,2,3（覆盖现有依赖）")
def edit(task_id: int, title: str | None, priority: str | None,
         status: str | None, agent: str | None, depends: str | None):
    """修改任务属性。"""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        click.echo(f"[red]任务 #{task_id} 不存在[/red]")
        return

    updates = {}
    if title:
        updates["title"] = title
    if priority:
        updates["priority"] = priority.upper()
    if status:
        updates["status"] = status.lower()
    if agent:
        updates["agent"] = agent.lower()
    if depends is not None:
        dep_list = [int(x.strip()) for x in depends.split(",") if x.strip()]
        updates["depends_on"] = json.dumps(dep_list) if dep_list else None

    if not updates:
        click.echo("[yellow]没有指定要修改的字段[/yellow]")
        return

    db.update_task(task_id, **updates)
    click.echo(f"[green][OK] 任务 #{task_id} 已更新[/green]")
    for k, v in updates.items():
        click.echo(f"  {k}: {v}")


# ── rm ─────────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_ids", type=int, nargs=-1)
@click.option("--force", "-f", is_flag=True, help="跳过确认直接删除")
def rm(task_ids: tuple[int, ...], force: bool):
    """删除一个或多个任务。"""
    db.init_db()
    if not task_ids:
        click.echo("[yellow]未指定任务 ID[/yellow]")
        return

    tasks = []
    for tid in task_ids:
        t = db.get_task(tid)
        if t:
            tasks.append(t)
        else:
            click.echo(f"[yellow]任务 #{tid} 不存在，已跳过[/yellow]")

    if not tasks:
        return

    click.echo(f"[cyan]将删除以下 {len(tasks)} 个任务：[/cyan]")
    for t in tasks:
        click.echo(f"  #{t['id']}  {t['title']}  [{t['status']}]")

    if not force:
        if not click.confirm("\n确认删除？"):
            click.echo("[yellow]已取消[/yellow]")
            return

    with db.get_conn() as conn:
        for t in tasks:
            conn.execute("DELETE FROM task_logs WHERE task_id = ?", (t["id"],))
            conn.execute("DELETE FROM tasks WHERE id = ?", (t["id"],))
        conn.commit()

    click.echo(f"[green][OK] 已删除 {len(tasks)} 个任务[/green]")


# ── find ────────────────────────────────────────────────────────────────────────

@click.command(context_settings={"allow_interspersed_args": False})
@click.argument("keyword", required=False)
@click.option("--project", "-p", callback=_resolve_project, help="限定项目")
@click.option("--status", "-s",
              type=click.Choice(["backlog", "in_progress", "done", "failed"], case_sensitive=False),
              help="按状态过滤")
@click.option("--priority", "--pri",
              type=click.Choice(["P0", "P1", "P2", "P3"], case_sensitive=False),
              help="按优先级过滤")
@click.option("--limit", "-n", type=int, default=20, help="最多显示数量")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def find(ctx: click.Context, keyword: str | None, project: str | None,
         status: str | None, priority: str | None, limit: int, json_mode: bool):
    """搜索任务（标题或内容包含关键词）。"""
    db.init_db()
    # 优先用本地 --json，否则用全局
    if not json_mode and ctx.parent:
        json_mode = ctx.parent.obj.get("json_mode", False)

    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list = []

    if project:
        sql += " AND project = ?"
        params.append(project)
    if status:
        sql += " AND status = ?"
        params.append(status.lower())
    if priority:
        sql += " AND priority = ?"
        params.append(priority.upper())
    if keyword:
        sql += " AND (title LIKE ? OR content LIKE ?)"
        like = f"%{keyword}%"
        params.extend([like, like])

    sql += " ORDER BY priority ASC, created_at DESC LIMIT ?"
    params.append(limit)

    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        if json_mode:
            click.echo(json.dumps({"tasks": [], "count": 0}, ensure_ascii=False, indent=2))
        else:
            click.echo("[yellow]没有找到匹配的任务[/yellow]")
        return

    if json_mode:
        click.echo(json.dumps({"tasks": [dict(r) for r in rows], "count": len(rows)},
                              ensure_ascii=False, indent=2))
        return

    click.echo(f"[cyan]找到 {len(rows)} 个任务：[/cyan]\n")
    for r in rows:
        status_color = {
            "backlog": "dim",
            "in_progress": "blue",
            "done": "green",
            "failed": "red",
        }.get(r["status"], "dim")

        click.echo(f"  #{r['id']}  [bold]{r['title']}[/bold]  "
                   f"[{status_color}]{r['status']}[/{status_color}]  "
                   f"{r['priority']}  {r['project']}")
        if keyword and r["content"]:
            snippet = r["content"][:80].replace("\n", " ")
            click.echo(f"    -> {snippet}...")
        click.echo()
