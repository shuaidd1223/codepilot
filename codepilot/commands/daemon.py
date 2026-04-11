"""Background loop that continuously drains runnable tasks."""

from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path

import click

from codepilot import db
from codepilot.commands.run import run_backlog
from codepilot.output import echo


def _resolve_project(ctx, param, value):
    if not value:
        return None
    db.init_db()
    proj = db.get_project(value)
    if not proj:
        echo(f"[red]错误: 项目 '{value}' 未注册[/red]")
        raise click.Abort()
    return value


LOCK_FILE = Path.home() / ".codepilot" / "daemon.lock"


def _acquire_lock() -> bool:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _is_process_alive(pid):
                return False
        except ValueError:
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _is_process_alive(pid: int) -> bool:
    system = platform.system().lower()
    if system == "windows":
        try:
            result = subprocess.run(
                ["powershell.exe", "-Command", f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).ProcessName"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


@click.command("daemon")
@click.option("--project", "-p", callback=_resolve_project, help="项目名称（不指定则监听所有项目）")
@click.option("--interval", type=int, default=60, help="轮询间隔（秒）")
@click.option("--max-concurrent", type=int, default=1, help="保留兼容参数，当前内置执行按单线程串行运行")
@click.option("--verbose", "-v", is_flag=True, help="输出更详细的调度信息")
@click.option(
    "--shell",
    type=click.Choice(["auto", "pwsh", "powershell", "bash", "zsh"], case_sensitive=False),
    default="auto",
    help="dispatch 模式下指定使用的 Shell",
)
@click.option(
    "--executor",
    type=click.Choice(["auto", "dispatch", "builtin"], case_sensitive=False),
    default="auto",
    help="执行器类型",
)
@click.option("--auto-commit/--no-auto-commit", default=True, help="内置执行器成功后自动提交当前任务")
def daemon(
    project: str | None,
    interval: int,
    max_concurrent: int,
    verbose: bool,
    shell: str,
    executor: str,
    auto_commit: bool,
):
    """Continuously poll backlog and run tasks in-process."""
    db.init_db()
    if not _acquire_lock():
        echo("[red]已有 daemon 实例运行中，退出[/red]")
        return

    try:
        _run_loop(project, interval, verbose, shell, executor, auto_commit)
    except KeyboardInterrupt:
        echo()
        echo("[yellow]守护进程收到停止信号，退出[/yellow]")
    finally:
        _release_lock()


def _run_loop(
    project: str | None,
    interval: int,
    verbose: bool,
    shell: str,
    executor: str,
    auto_commit: bool,
) -> None:
    echo(
        f"[cyan]CodePilot Daemon[/cyan]  项目: {project or 'all'}  间隔: {interval}s  "
        f"执行器: {executor}\n"
    )
    echo("[yellow]守护进程运行中，按 Ctrl+C 停止[/yellow]")
    click.echo()

    while True:
        stats = _get_combined_stats(project)
        timestamp = time.strftime("%H:%M:%S")

        if stats["backlog"] == 0:
            if verbose:
                echo(f"[dim][{timestamp}] 空闲，backlog: 0[/dim]")
            time.sleep(interval)
            continue

        echo(
            f"[bold][{timestamp}][/bold] [green]backlog: {stats['backlog']}[/green]  "
            f"[blue]in-progress: {stats['in_progress']}[/blue]"
        )

        targets = [project] if project else [proj["name"] for proj in db.list_projects()]
        for target in targets:
            result = run_backlog(
                target,
                once=True,
                limit=1,
                dry_run=False,
                shell=shell,
                executor=executor,
                auto_commit=auto_commit,
            )
            if verbose:
                echo(
                    f"[dim][{timestamp}] {target}: processed={result['processed']} "
                    f"done={result['done']} failed={result['failed']} "
                    f"requeued={result['requeued']}[/dim]"
                )
        time.sleep(interval)


def _get_combined_stats(project: str | None) -> dict:
    if project:
        return db.get_task_stats(project)

    combined = {"backlog": 0, "in_progress": 0, "done": 0, "failed": 0, "total": 0}
    for proj in db.list_projects():
        stats = db.get_task_stats(proj["name"])
        for key in combined:
            combined[key] += stats.get(key, 0)
    return combined
