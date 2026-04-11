"""codepilot daemon 命令：常驻守护进程，自动触发 run 调度任务.

跨平台支持：Windows (PowerShell) / Linux/macOS (bash/zsh)
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import click

from codepilot import db


# ═══════════════════════════════════════════════════════════════════════════════
# 跨平台锁文件
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_project(ctx, param, value):
    """自动补全项目名称，支持空值."""
    if not value:
        return None
    db.init_db()
    proj = db.get_project(value)
    if not proj:
        click.echo(f"[red]错误: 项目 '{value}' 未注册[/red]")
        raise click.Abort()
    return value


LOCK_FILE = Path.home() / ".codepilot" / "daemon.lock"


def _acquire_lock() -> bool:
    """尝试获取锁，返回是否成功."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # 检查进程是否还活着
            if _is_process_alive(pid):
                return False
        except ValueError:
            pass

    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _is_process_alive(pid: int) -> bool:
    """检查进程是否存活."""
    system = platform.system().lower()

    if system == "windows":
        try:
            # Windows: 使用 PowerShell 检查进程
            result = subprocess.run(
                ["powershell.exe", "-Command",
                 f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).ProcessName"],
                capture_output=True,
                timeout=5,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return bool(result.stdout.strip())
        except Exception:
            return False
    else:
        # Unix: 使用 kill -0
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _release_lock():
    """释放锁文件."""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# 子进程运行
# ═══════════════════════════════════════════════════════════════════════════════

def _run_subprocess(args: list[str]) -> tuple[int, str, str]:
    """运行子进程，返回 (returncode, stdout, stderr)."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout or "", result.stderr or ""


def _get_python_executable() -> str:
    """获取 Python 解释器路径."""
    return sys.executable


# ═══════════════════════════════════════════════════════════════════════════════
# CLI 命令
# ═══════════════════════════════════════════════════════════════════════════════

@click.command()
@click.option(
    "--project", "-p",
    callback=_resolve_project,
    help="项目名称（不指定则监听所有项目）",
)
@click.option("--interval", type=int, default=60, help="轮询间隔（秒），默认 60")
@click.option("--max-concurrent", type=int, default=1, help="最大并发任务数，默认 1")
@click.option("--verbose", "-v", is_flag=True, help="详细输出")
@click.option(
    "--shell",
    type=click.Choice(["auto", "pwsh", "powershell", "bash", "zsh"], case_sensitive=False),
    default="auto",
    help="指定使用的 Shell（默认自动检测）",
)
def daemon(
    project: str | None,
    interval: int,
    max_concurrent: int,
    verbose: bool,
    shell: str,
):
    """
    启动常驻守护进程，监听 backlog 并自动触发 run 调度.

    跨平台支持：
    - Windows: PowerShell 7 / PowerShell 5 / cmd
    - Linux/macOS: zsh / bash / sh

    行为：
    - backlog > 0 且 in-progress < max-concurrent 时，自动调用 run
    - 有任务执行中时等待（不重复触发），直到任务完成
    - 支持 Ctrl+C 优雅退出
    """
    db.init_db()

    if not _acquire_lock():
        click.echo("[red]已有 daemon 实例运行中，退出[/red]")
        return

    try:
        _run_loop(project, interval, max_concurrent, verbose, shell)
    except KeyboardInterrupt:
        click.echo("\n[yellow]守护进程收到停止信号，退出[/yellow]")
    finally:
        _release_lock()


def _run_loop(
    project: str | None,
    interval: int,
    max_concurrent: int,
    verbose: bool,
    shell: str,
):
    """守护主循环."""
    project_str = project or "all"
    click.echo(
        f"[cyan]CodePilot Daemon[/cyan]  "
        f"项目: {project_str}  "
        f"间隔: {interval}s  "
        f"并发上限: {max_concurrent}\n"
    )
    click.echo("[yellow]守护进程运行中，按 Ctrl+C 停止[/yellow]\n")

    python_exe = _get_python_executable()

    while True:
        stats = _get_combined_stats(project)
        pending = stats["backlog"]
        in_progress = stats["in_progress"]

        timestamp = time.strftime("%H:%M:%S")

        if in_progress >= max_concurrent:
            # 有任务在跑，等待
            if verbose:
                click.echo(
                    f"[{timestamp}] [blue]in-progress: {in_progress}[/blue]  "
                    f"[dim]等待任务完成...[/dim]"
                )
            time.sleep(interval)
            continue

        if pending == 0:
            click.echo(f"[{timestamp}] [dim]空闲，backlog: 0[/dim]")
            time.sleep(interval)
            continue

        # 有待执行任务，触发 run
        click.echo(
            f"[{timestamp}] [green]backlog: {pending}[/green]  "
            f"[blue]in-progress: {in_progress}[/blue]"
        )
        click.echo(f"[cyan]  -> 触发 run...[/cyan]")

        p_args = [python_exe, "-m", "codepilot", "run", "--once"]
        if project:
            p_args.extend(["--project", project])
        if shell != "auto":
            p_args.extend(["--shell", shell])

        if verbose:
            result = subprocess.run(p_args)
            exit_code = result.returncode
        else:
            result = subprocess.run(
                p_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            exit_code = result.returncode
            if result.stdout:
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        click.echo(f"  {line}")

        # run 完成后短暂等待再检查状态
        time.sleep(3)

        # 再检查一次最新状态
        stats2 = _get_combined_stats(project)
        if verbose:
            click.echo(
                f"[{timestamp}] [dim]Run 退出码: {exit_code}  "
                f"backlog: {stats2['backlog']}  "
                f"done: {stats2['done']}  "
                f"in-progress: {stats2['in_progress']}[/dim]"
            )


def _get_combined_stats(project: str | None) -> dict:
    """获取项目统计，支持单项目或全部."""
    if project:
        return db.get_task_stats(project)
    else:
        # 合并所有项目的统计
        combined = {"backlog": 0, "in_progress": 0, "done": 0, "failed": 0, "total": 0}
        for proj in db.list_projects():
            s = db.get_task_stats(proj["name"])
            for k in combined:
                combined[k] += s.get(k, 0)
        return combined
