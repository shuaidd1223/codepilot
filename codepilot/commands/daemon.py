"""Background loop that continuously drains runnable tasks."""

from __future__ import annotations

import os
import time
from pathlib import Path

import click

import threading
from concurrent.futures import ThreadPoolExecutor

from codepilot import db
from codepilot.commands.inspect import run_inspection
from codepilot.commands.run import run_backlog
from codepilot.config import load_project_config, resolve_planner
from codepilot.output import echo, safe
from codepilot.paths import global_storage_root
from codepilot.runtime import is_process_alive, reap_stalled_tasks
from codepilot.webui import start_ui_server


def _resolve_project(ctx, param, value):
    if not value:
        return None
    db.init_db()
    proj = db.get_project(value)
    if not proj:
        echo(f"[red]错误: 项目 '{value}' 未注册[/red]")
        raise click.Abort()
    return value


DAEMON_STATE_DIR = global_storage_root() / "daemon"
LOCK_FILE = DAEMON_STATE_DIR / "daemon.lock"
HEARTBEAT_FILE = DAEMON_STATE_DIR / "daemon.heartbeat"


def _tick_heartbeat() -> None:
    """Write ISO timestamp to the heartbeat file so outside observers
    (e.g. the Web UI banner) can tell the daemon is alive. Called at the
    top of every daemon loop iteration — silently ignores IO failures so a
    transient disk glitch never takes down the loop itself."""
    from datetime import datetime
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except Exception:
        pass


def _start_ui_background(port: int = 8766):
    """Start Web UI in a daemon thread so it lives as long as the daemon."""
    try:
        server = start_ui_server(host="127.0.0.1", port=port, open_browser=False)
    except OSError as exc:
        echo(f"[yellow]Web UI 启动失败（端口 {port} 可能被占用）：{safe(exc)}[/yellow]")
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    echo(f"[cyan]Web UI[/cyan]  http://127.0.0.1:{port}/")
    return server


def _acquire_lock() -> bool:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if is_process_alive(pid):
                return False
        except ValueError:
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        HEARTBEAT_FILE.unlink(missing_ok=True)
    except Exception:
        pass


@click.command("daemon")
@click.option("--project", "-p", callback=_resolve_project, help="项目名称（不指定则监听所有项目）")
@click.option("--interval", type=int, default=60, help="轮询间隔（秒）")
@click.option(
    "--max-concurrent",
    type=int,
    default=1,
    help="最多并行跑多少个项目（单项目内仍串行，防止 git 工作区打架）",
)
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
@click.option("--ui/--no-ui", "enable_ui", default=True, help="同时启动 Web UI（默认开启）")
@click.option("--ui-port", type=int, default=8766, help="Web UI 端口")
def daemon(
    project: str | None,
    interval: int,
    max_concurrent: int,
    verbose: bool,
    shell: str,
    executor: str,
    auto_commit: bool,
    enable_ui: bool,
    ui_port: int,
):
    """持续轮询 backlog 并在进程内执行任务."""
    db.init_db()
    if not _acquire_lock():
        echo("[red]已有 daemon 实例运行中，退出[/red]")
        return

    ui_server = None
    try:
        if enable_ui:
            ui_server = _start_ui_background(ui_port)
        _run_loop(project, interval, verbose, shell, executor, auto_commit, max_concurrent)
    except KeyboardInterrupt:
        echo()
        echo("[yellow]守护进程收到停止信号，退出[/yellow]")
    finally:
        if ui_server:
            ui_server.shutdown()
            ui_server.server_close()
            echo("[dim]Web UI 已停止[/dim]")
        _release_lock()


def _run_loop(
    project: str | None,
    interval: int,
    verbose: bool,
    shell: str,
    executor: str,
    auto_commit: bool,
    max_concurrent: int = 1,
) -> None:
    echo(
        f"[cyan]CodePilot Daemon[/cyan]  项目: {project or 'all'}  间隔: {interval}s  "
        f"执行器: {executor}  并行度: {max_concurrent}\n"
    )
    echo("[yellow]守护进程运行中，按 Ctrl+C 停止[/yellow]")
    click.echo()

    last_inspect_at: dict[str, float] = {}

    while True:
        _tick_heartbeat()
        reaped = reap_stalled_tasks(project)
        for task in reaped:
            echo(f"[yellow]已回收卡住任务 #{task['id']}：{task['title']}[/yellow]")

        _maybe_run_inspect(project, last_inspect_at, verbose)

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

        def _drain(name: str) -> dict:
            return run_backlog(
                name,
                once=True,
                limit=1,
                dry_run=False,
                shell=shell,
                executor=executor,
                auto_commit=auto_commit,
            )

        if max_concurrent > 1 and len(targets) > 1:
            with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
                results = list(pool.map(_drain, targets))
        else:
            results = [_drain(name) for name in targets]

        if verbose:
            for name, result in zip(targets, results):
                echo(
                    f"[dim][{timestamp}] {name}: processed={result['processed']} "
                    f"done={result['done']} failed={result['failed']} "
                    f"requeued={result['requeued']}[/dim]"
                )
        time.sleep(interval)


def _maybe_run_inspect(project: str | None, last_at: dict[str, float], verbose: bool) -> None:
    """Run periodic inspection for each configured project when the interval elapses."""
    now = time.monotonic()
    targets = [project] if project else [proj["name"] for proj in db.list_projects()]
    for name in targets:
        proj = db.get_project(name)
        if not proj:
            continue
        try:
            cfg = load_project_config(Path(proj["path"]))
        except Exception:
            continue
        ins = getattr(cfg, "inspect", None)
        if not ins or not ins.enabled:
            continue
        elapsed = now - last_at.get(name, 0.0)
        if last_at.get(name) and elapsed < ins.interval_seconds:
            continue
        last_at[name] = now
        stamp = time.strftime("%H:%M:%S")
        echo(f"[cyan][{stamp}] 巡检 {name}[/cyan]")
        try:
            result = run_inspection(
                {"name": name, "path": proj["path"]},
                max_new_tasks=ins.max_new_tasks_per_round,
                signals=ins.signals,
                auto_execute=ins.auto_execute,
                priority=ins.priority,
                agent="codex",                # 执行器：写代码默认 codex
                planner=resolve_planner(cfg, "inspect"),
            )
        except Exception as exc:
            echo(f"[yellow]巡检 {name} 失败：{safe(exc)}[/yellow]")
            continue
        if result.get("error"):
            echo(f"[yellow]巡检 {name}：{result['error']}[/yellow]")
            continue
        created = result.get("created") or []
        if created:
            echo(f"[green][{stamp}] {name} 新增 {len(created)} 条建议[/green]")
            for task in created:
                echo(f"  #{task['id']}  {task['title']}  [{task['priority']}]")
        elif verbose:
            echo(f"[dim][{stamp}] {name} 巡检无新建议[/dim]")


def _get_combined_stats(project: str | None) -> dict:
    if project:
        return db.get_task_stats(project)

    combined = {"backlog": 0, "in_progress": 0, "done": 0, "failed": 0, "cancelled": 0, "total": 0}
    for proj in db.list_projects():
        stats = db.get_task_stats(proj["name"])
        for key in combined:
            combined[key] += stats.get(key, 0)
    return combined
