"""codepilot 任务管理命令：edit / rm / done / retry / find."""

from __future__ import annotations

import json
from pathlib import Path

import click

from codepilot import db
from codepilot.commands.status import _resolve_project
from codepilot.output import echo
from codepilot.runtime import (
    clear_task_runtime,
    is_process_alive,
    request_task_stop,
    stop_process_tree,
    stop_worktree_leftovers,
)


# ── done ──────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_id", type=int)
@click.option("--message", "-m", default="", help="完成备注/交付说明")
def done(task_id: int, message: str):
    """手动标记任务为完成（不运行 Agent）。"""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        echo(f"[red]任务 #{task_id} 不存在[/red]")
        return

    from datetime import datetime
    updates = {"status": "done", "completed_at": datetime.now().isoformat()}
    if message:
        updates["delivery_record"] = message
    clear_task_runtime(task_id, stop_requested=0, stop_reason=None, **updates)
    echo(f"[green][OK] 任务 #{task_id} 已标记为 done[/green]  {task['title']}")


# ── retry ─────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_id", type=int)
def retry(task_id: int):
    """手动重试指定任务：重置运行态并重新放回 backlog。"""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        echo(f"[red]任务 #{task_id} 不存在[/red]")
        return

    if task["status"] == "in_progress":
        echo(f"[yellow]任务 #{task_id} 正在运行中，请先执行 stop 再重试[/yellow]")
        return

    if task["status"] == "done":
        echo(f"[yellow]任务 #{task_id} 已完成；如需重跑，请先手动修改状态后再执行[/yellow]")
        return

    updated = db.reset_task_for_retry(task_id)
    echo(
        f"[green][OK] 任务 #{task_id} 已重新放回 backlog[/green]  "
        f"{task['title']}  (retry_count: {task.get('retry_count') or 0} -> {updated.get('retry_count') or 0})"
    )
    click.echo(f"  项目: {task['project']}")
    click.echo(f"  下一步: codepilot run -p {task['project']}")


# ── cancel ────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_ids", type=int, nargs=-1, required=True)
@click.option("--message", "-m", default="", help="取消原因")
def cancel(task_ids: tuple[int, ...], message: str):
    """取消一个或多个任务（保留记录，不删除）。"""
    db.init_db()
    count = 0
    for tid in task_ids:
        task = db.get_task(tid)
        if not task:
            echo(f"[yellow]任务 #{tid} 不存在，跳过[/yellow]")
            continue
        if task["status"] == "done":
            echo(f"[yellow]任务 #{tid} 已完成，无法取消[/yellow]")
            continue
        if task["status"] == "in_progress":
            pid = task.get("active_pid")
            if pid:
                stop_process_tree(pid)
            reason = message or f"任务 #{tid} 已取消"
            request_task_stop(tid, reason)
        from datetime import datetime
        clear_task_runtime(
            tid,
            status="cancelled",
            completed_at=datetime.now().isoformat(),
            error_message=message or "手动取消",
            stop_requested=0,
            stop_reason=None,
        )
        # Sweep any long-lived dev servers (next dev / vite / etc.) the
        # builder agent left inside the task's worktree.
        wt = (task or {}).get("worktree_path")
        project_path = (task or {}).get("project_path")
        try:
            if wt and wt != project_path:
                stop_worktree_leftovers(wt, wait_seconds=3)
        except Exception:
            pass
        echo(f"[yellow]已取消 #{tid}[/yellow]  {task['title']}")
        count += 1
    if count:
        echo(f"[green][OK] 共取消 {count} 个任务[/green]")


# ── resume ────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_ids", type=int, nargs=-1, required=True)
def resume(task_ids: tuple[int, ...]):
    """恢复 cancelled/failed 任务到 backlog，等待重新执行。"""
    db.init_db()
    count = 0
    for tid in task_ids:
        task = db.get_task(tid)
        if not task:
            echo(f"[yellow]任务 #{tid} 不存在，跳过[/yellow]")
            continue
        if task["status"] not in ("cancelled", "failed"):
            echo(f"[yellow]任务 #{tid} 状态为 {task['status']}，只有 cancelled/failed 可恢复[/yellow]")
            continue
        clear_task_runtime(
            tid,
            status="backlog",
            error_message=None,
            stop_requested=0,
            stop_reason=None,
        )
        echo(f"[green]已恢复 #{tid}[/green]  {task['title']} → backlog")
        count += 1
    if count:
        echo(f"[green][OK] 共恢复 {count} 个任务到 backlog[/green]")


# ── edit ───────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_id", type=int)
@click.option("--title", "-t", help="修改标题")
@click.option("--priority", "-p",
               type=click.Choice(["P0", "P1", "P2", "P3"], case_sensitive=False),
               help="修改优先级")
@click.option("--status", "-s",
               type=click.Choice(["backlog", "in_progress", "done", "failed", "cancelled"], case_sensitive=False),
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
        echo(f"[red]任务 #{task_id} 不存在[/red]")
        return

    updates = {}
    if title:
        updates["title"] = title
    if priority:
        updates["priority"] = priority.upper()
    if status:
        normalized_status = status.lower()
        updates["status"] = normalized_status
        if normalized_status != "in_progress":
            updates.update(
                {
                    "run_phase": None,
                    "heartbeat_at": None,
                    "active_pid": None,
                    "current_log_path": None,
                    "last_output": None,
                    "stop_requested": 0,
                    "stop_reason": None,
                }
            )
    if agent:
        updates["agent"] = agent.lower()
    if depends is not None:
        dep_list = [int(x.strip()) for x in depends.split(",") if x.strip()]
        updates["depends_on"] = json.dumps(dep_list) if dep_list else None

    if not updates:
        echo("[yellow]没有指定要修改的字段[/yellow]")
        return

    db.update_task(task_id, **updates)
    echo(f"[green][OK] 任务 #{task_id} 已更新[/green]")
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
        echo("[yellow]未指定任务 ID[/yellow]")
        return

    tasks = []
    for tid in task_ids:
        t = db.get_task(tid)
        if t:
            tasks.append(t)
        else:
            echo(f"[yellow]任务 #{tid} 不存在，已跳过[/yellow]")

    if not tasks:
        return

    echo(f"[cyan]将删除以下 {len(tasks)} 个任务：[/cyan]")
    for t in tasks:
        click.echo(f"  #{t['id']}  {t['title']}  [{t['status']}]")

    if not force:
        if not click.confirm("\n确认删除？"):
            echo("[yellow]已取消[/yellow]")
            return

    with db.get_conn() as conn:
        for t in tasks:
            conn.execute("DELETE FROM task_logs WHERE task_id = ?", (t["id"],))
            conn.execute("DELETE FROM tasks WHERE id = ?", (t["id"],))
        conn.commit()

    echo(f"[green][OK] 已删除 {len(tasks)} 个任务[/green]")


# ── find ────────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("keyword", required=False)
@click.option("--project", "-p", callback=_resolve_project, help="限定项目")
@click.option("--status", "-s",
              type=click.Choice(["backlog", "in_progress", "done", "failed", "cancelled"], case_sensitive=False),
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
            echo("[yellow]没有找到匹配的任务[/yellow]")
        return

    if json_mode:
        click.echo(json.dumps({"tasks": [dict(r) for r in rows], "count": len(rows)},
                              ensure_ascii=False, indent=2))
        return

    echo(f"[cyan]找到 {len(rows)} 个任务：[/cyan]")
    click.echo()
    for r in rows:
        status_color = {
            "backlog": "dim",
            "in_progress": "blue",
            "done": "green",
            "failed": "red",
            "cancelled": "magenta",
        }.get(r["status"], "dim")

        echo(
            f"  #{r['id']}  [bold]{r['title']}[/bold]  "
            f"[{status_color}]{r['status']}[/{status_color}]  "
            f"{r['priority']}  {r['project']}"
        )
        if keyword and r["content"]:
            snippet = r["content"][:80].replace("\n", " ")
            click.echo(f"    -> {snippet}...")
        click.echo()


# ── stop ───────────────────────────────────────────────────────────────────────

@click.command()
@click.argument("task_id", type=int)
@click.option("--message", "-m", default="", help="停止原因")
def stop(task_id: int, message: str):
    """停止一个正在运行的任务。"""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        echo(f"[red]任务 #{task_id} 不存在[/red]")
        return

    if task["status"] != "in_progress":
        echo(f"[yellow]任务 #{task_id} 当前不是运行中状态，无需停止[/yellow]")
        return

    reason = message.strip() or f"任务 #{task_id} 已收到手动停止请求"
    request_task_stop(task_id, reason)

    pid = task.get("active_pid")
    if pid:
        stop_process_tree(pid)

    refreshed = db.get_task(task_id) or task
    if refreshed.get("active_pid") and is_process_alive(refreshed.get("active_pid")) and refreshed.get("status") == "in_progress":
        echo(f"[yellow]已向任务 #{task_id} 发送停止请求，等待执行器收尾[/yellow]")
        return

    from datetime import datetime

    clear_task_runtime(
        task_id,
        status="cancelled",
        completed_at=refreshed.get("completed_at") or datetime.now().isoformat(),
        error_message=reason,
        stop_requested=0,
        stop_reason=None,
    )
    # Sweep any dev servers still inside the task's worktree.
    wt = (refreshed or task or {}).get("worktree_path")
    project_path = (refreshed or task or {}).get("project_path")
    try:
        if wt and wt != project_path:
            stop_worktree_leftovers(wt, wait_seconds=3)
    except Exception:
        pass
    echo(f"[yellow]任务 #{task_id} 已停止[/yellow]  {task['title']}")


# ── logs ───────────────────────────────────────────────────────────────────────

def _render_log_text(text: str, tail: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-tail:]) if tail > 0 else text


@click.command()
@click.argument("task_id", type=int)
@click.option("--dry-run", is_flag=True, help="只列出候选 PID，不真的杀")
def sweep(task_id: int, dry_run: bool):
    """清理任务 worktree 里遗留的长生命进程（next dev / vite / npm run dev 等）。"""
    from codepilot.runtime import find_worktree_processes
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        echo(f"[red]任务 #{task_id} 不存在[/red]")
        return
    wt = task.get("worktree_path")
    project_path = task.get("project_path")
    if not wt:
        echo(f"[yellow]任务 #{task_id} 没有 worktree，不需要 sweep[/yellow]")
        return
    if wt == project_path:
        echo(f"[yellow]任务 #{task_id} 直接运行在项目根目录（没有隔离 worktree），跳过 sweep 以避免误杀你自己的进程[/yellow]")
        return
    pids = find_worktree_processes(wt)
    if not pids:
        echo(f"[dim]worktree {wt} 没有遗留进程[/dim]")
        return
    echo(f"[cyan]候选 PID:[/cyan] {', '.join(str(p) for p in pids)}")
    if dry_run:
        return
    killed = stop_worktree_leftovers(wt, wait_seconds=4)
    if killed:
        echo(f"[green][OK] 已清理 {len(killed)} 个进程[/green]  PID={','.join(str(p) for p in killed)}")


@click.command()
@click.argument("task_id", type=int)
@click.option("--tail", "-n", type=int, default=80, help="仅显示最后 N 行")
@click.option("--full", is_flag=True, help="显示完整日志")
def logs(task_id: int, tail: int, full: bool):
    """查看任务日志；运行中的任务优先显示实时日志。"""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        echo(f"[red]任务 #{task_id} 不存在[/red]")
        return

    live_log = task.get("current_log_path")
    if task.get("status") == "in_progress" and live_log and Path(live_log).exists():
        echo(f"[cyan]实时日志[/cyan]  {live_log}")
        text = Path(live_log).read_text(encoding="utf-8", errors="replace")
        click.echo(text if full else _render_log_text(text, tail))
        return

    task_logs = db.list_task_logs(task_id)
    if not task_logs:
        snippet = task.get("last_output") or ""
        if snippet:
            echo(f"[cyan]最近输出[/cyan]")
            click.echo(snippet if full else _render_log_text(snippet, tail))
        else:
            echo("[yellow]这个任务还没有可用日志[/yellow]")
        return

    for entry in task_logs:
        echo(
            f"[cyan]{entry['phase']}[/cyan]  agent={entry.get('agent') or '-'}  "
            f"exit={entry.get('exit_code') if entry.get('exit_code') is not None else '-'}"
        )
        text = entry.get("output") or ""
        if text:
            click.echo(text if full else _render_log_text(text, tail))
        click.echo()
