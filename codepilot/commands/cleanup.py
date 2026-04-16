"""codepilot cleanup — purge stale/zombie tasks and orphaned log files."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import click

from codepilot import db
from codepilot.output import echo, safe
from codepilot.runtime import is_process_alive


# ── Core logic ──────────────────────────────────────────────────────────────


def _mark_stale_tasks(
    project: str,
    stale_minutes: int,
    *,
    dry_run: bool = False,
) -> list[dict]:
    """Find in_progress tasks with expired heartbeat and mark them failed."""
    candidates = db.find_stale_in_progress(project, stale_minutes)
    results: list[dict] = []
    now_iso = datetime.now().isoformat()

    for task in candidates:
        # Skip tasks whose process is still alive.
        if is_process_alive(task.get("active_pid")):
            continue

        entry = {
            "id": task["id"],
            "title": task["title"],
            "heartbeat_at": task.get("heartbeat_at"),
            "active_pid": task.get("active_pid"),
            "action": "mark_failed",
        }
        results.append(entry)

        if not dry_run:
            db.update_task(
                task["id"],
                status="failed",
                error_message=f"cleanup: 心跳超过 {stale_minutes} 分钟且进程不存在，标记为 failed。",
                completed_at=now_iso,
                run_phase=None,
                heartbeat_at=None,
                active_pid=None,
                current_log_path=None,
                last_output=None,
                stop_requested=0,
                stop_reason=None,
            )

    return results


def _clear_orphan_log_paths(
    project: str,
    *,
    dry_run: bool = False,
) -> list[dict]:
    """Find tasks whose current_log_path points to a missing file and clear it."""
    candidates = db.find_orphan_log_paths(project)
    results: list[dict] = []

    for task in candidates:
        log_path = task.get("current_log_path") or ""
        if not log_path:
            continue
        if Path(log_path).exists():
            continue

        entry = {
            "id": task["id"],
            "title": task["title"],
            "missing_path": log_path,
            "action": "clear_log_path",
        }
        results.append(entry)

        if not dry_run:
            db.update_task(task["id"], current_log_path=None)

    return results


def _purge_old_logs(
    project: str,
    retention_days: int,
    *,
    dry_run: bool = False,
) -> tuple[list[dict], int]:
    """Delete log files of done tasks older than *retention_days*.

    Returns (entries, freed_bytes).
    """
    candidates = db.find_old_done_tasks(project, retention_days)
    results: list[dict] = []
    freed = 0

    for task in candidates:
        log_path = task.get("current_log_path") or ""
        if not log_path:
            continue

        p = Path(log_path)
        file_size = 0
        if p.exists():
            try:
                file_size = p.stat().st_size
            except OSError:
                file_size = 0

        entry = {
            "id": task["id"],
            "title": task["title"],
            "log_path": log_path,
            "size": file_size,
            "action": "delete_log",
        }
        results.append(entry)

        if not dry_run:
            if p.exists():
                try:
                    p.unlink()
                    freed += file_size
                except OSError:
                    pass
            db.update_task(task["id"], current_log_path=None)
        else:
            freed += file_size

    return results, freed


def _format_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


# ── Click command ───────────────────────────────────────────────────────────


@click.command()
@click.option("-p", "--project", required=True, help="项目名称")
@click.option(
    "--stale-minutes",
    type=int,
    default=None,
    help="心跳超时分钟数（默认读取 AGENTS.toml 中的 stale_minutes，否则 30）",
)
@click.option(
    "--retention-days",
    type=int,
    default=30,
    show_default=True,
    help="已完成任务日志保留天数",
)
@click.option("--dry-run", is_flag=True, default=False, help="仅打印将要处理的条目，不执行")
@click.pass_context
def cleanup(
    ctx: click.Context,
    project: str,
    stale_minutes: Optional[int],
    retention_days: int,
    dry_run: bool,
):
    """清理失联/僵尸任务与过期日志文件。"""
    json_mode = (ctx.parent.obj or {}).get("json_mode", False) if ctx.parent else False

    # Resolve stale_minutes from config if not provided.
    if stale_minutes is None:
        try:
            from codepilot.config import load_config

            cfg = load_config()
            stale_minutes = cfg.stale_minutes if cfg else 30
        except Exception:
            stale_minutes = 30

    # Verify project exists.
    proj = db.get_project(project)
    if not proj:
        if json_mode:
            click.echo(json.dumps({"error": f"项目 '{project}' 不存在"}, ensure_ascii=False))
        else:
            echo(f"[red]错误：项目 '{safe(project)}' 不存在。[/red]")
        ctx.exit(1)
        return

    # Run three cleanup passes.
    stale_results = _mark_stale_tasks(project, stale_minutes, dry_run=dry_run)
    orphan_results = _clear_orphan_log_paths(project, dry_run=dry_run)
    purge_results, freed_bytes = _purge_old_logs(project, retention_days, dry_run=dry_run)

    # ── JSON output ─────────────────────────────────────────────────────────
    if json_mode:
        payload = {
            "dry_run": dry_run,
            "stale_tasks": stale_results,
            "orphan_log_paths": orphan_results,
            "purged_logs": purge_results,
            "summary": {
                "stale_marked_failed": len(stale_results),
                "orphan_paths_cleared": len(orphan_results),
                "logs_deleted": len(purge_results),
                "bytes_freed": freed_bytes,
            },
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    # ── Rich output ─────────────────────────────────────────────────────────
    mode_label = "[yellow](dry-run)[/yellow] " if dry_run else ""
    echo()
    echo(f"[bold]codepilot cleanup[/bold]  {mode_label}项目: {safe(project)}")
    echo("─" * 54)

    # 1) Stale tasks
    echo(f"\n[bold]失联任务[/bold]（heartbeat > {stale_minutes} 分钟 & 进程不存在）")
    if stale_results:
        for e in stale_results:
            echo(f"  [red]✘[/red]  #{e['id']}  {safe(e['title'])}  (heartbeat: {e.get('heartbeat_at', '-')})")
    else:
        echo("  [green]✔[/green]  无")

    # 2) Orphan log paths
    echo(f"\n[bold]孤立日志路径[/bold]（文件已丢失）")
    if orphan_results:
        for e in orphan_results:
            echo(f"  [red]✘[/red]  #{e['id']}  {safe(e['title'])}  → {safe(e['missing_path'])}")
    else:
        echo("  [green]✔[/green]  无")

    # 3) Old logs
    echo(f"\n[bold]过期日志[/bold]（done 任务 > {retention_days} 天）")
    if purge_results:
        for e in purge_results:
            echo(f"  [red]✘[/red]  #{e['id']}  {safe(e['title'])}  {_format_bytes(e['size'])}  → {safe(e['log_path'])}")
    else:
        echo("  [green]✔[/green]  无")

    # Summary
    total_actions = len(stale_results) + len(orphan_results) + len(purge_results)
    echo("\n" + "─" * 54)
    verb = "将处理" if dry_run else "已处理"
    echo(
        f"  {verb} {total_actions} 项：失效 {len(stale_results)}、"
        f"孤立路径 {len(orphan_results)}、过期日志 {len(purge_results)}"
    )
    if freed_bytes:
        freed_label = "可释放" if dry_run else "已释放"
        echo(f"  {freed_label}空间：{_format_bytes(freed_bytes)}")
    echo()
