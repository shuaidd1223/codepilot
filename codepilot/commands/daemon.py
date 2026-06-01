"""Background loop that continuously drains runnable tasks."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import click

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

from codepilot.storage import database as db
from codepilot.commands.feishu import ensure_service_running_if_enabled
from codepilot.commands.run import run_backlog
from codepilot.core.output import echo, safe
from codepilot.core.paths import _slugify_project_name, global_storage_root
from codepilot.core.runtime import codepilot_command, is_process_alive, reap_stalled_tasks
from codepilot.core.service_launcher import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    DETACHED_PROCESS,
    append_log_header,
    hidden_windows_startupinfo,
    spawn_detached_command_via_launcher as _spawn_detached_command_via_launcher,
)
from codepilot.core.text_decode import decode_subprocess_text


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
DEFAULT_DAEMON_STALE_AFTER_SECONDS = 120


def _service_dir(project: str | None) -> Path:
    if not project:
        return DAEMON_STATE_DIR / "_global"
    return DAEMON_STATE_DIR / _slugify_project_name(project)


def _service_log_path(project: str | None) -> Path:
    return _service_dir(project) / "daemon.log"


def _service_scope(project: str | None) -> str:
    return (project or "").strip()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _state_heartbeat_stale(
    state: dict | None,
    *,
    stale_after_seconds: int = DEFAULT_DAEMON_STALE_AFTER_SECONDS,
) -> bool:
    if not state:
        return False
    heartbeat = str(state.get("heartbeat_at") or "").strip()
    if not heartbeat:
        return False
    try:
        delta = (datetime.now() - datetime.fromisoformat(heartbeat)).total_seconds()
    except ValueError:
        return False
    return delta > max(0, int(stale_after_seconds))


def _service_targets(project: str | None = None) -> list[int]:
    state = db.get_service_state("daemon", _service_scope(project))
    if not state:
        return []
    try:
        pid = int(state.get("pid") or 0)
    except Exception:
        pid = 0
    if pid and is_process_alive(pid) and not _state_heartbeat_stale(state):
        return [pid]
    return []


def daemon_service_status(
    project: str | None = None,
    *,
    stale_after_seconds: int = DEFAULT_DAEMON_STALE_AFTER_SECONDS,
) -> dict:
    log_file = _service_log_path(project)
    state = db.get_service_state("daemon", _service_scope(project))
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    try:
        pid = int(state.get("pid") or 0) if state else 0
    except Exception:
        pid = 0
    running = bool(pid and is_process_alive(pid))
    live_log = str(state.get("log_path") or "") if state else ""
    stopping = bool(state and str(state.get("status") or "").strip().lower() == "stopping")
    stale = bool(running and _state_heartbeat_stale(state, stale_after_seconds=stale_after_seconds))
    if stale:
        _cleanup_service_files(project)
        running = False
        stopping = False
    return {
        "running": running,
        "stopping": bool(running and stopping),
        "pid": pid if running else 0,
        "project": meta.get("project") or "",
        "started_at": meta.get("started_at") or "",
        "log": live_log or str(log_file),
        "stale": stale,
    }


def _cleanup_service_files(project: str | None = None) -> None:
    db.clear_service_state("daemon", _service_scope(project))


def _write_meta(pid: int, *, project: str | None, interval: int, executor: str) -> None:
    payload = {
        "pid": int(pid),
        "project": project or "",
        "interval": int(interval),
        "executor": executor,
        "started_at": _now_iso(),
    }
    db.upsert_service_state(
        "daemon",
        _service_scope(project),
        pid=int(pid),
        status="running",
        log_path=str(_service_log_path(project)),
        heartbeat_at=_now_iso(),
        meta=payload,
    )


def _spawn_detached_daemon(
    *,
    project: str | None,
    interval: int,
    max_concurrent: int,
    shell: str,
    executor: str,
    auto_commit: bool,
) -> subprocess.Popen:
    log_file = _service_log_path(project)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_file, "ab")
    try:
        log_fp.write(f"\n--- start {_now_iso()} project={project or 'all'} interval={interval} ---\n".encode("utf-8"))
        log_fp.flush()
    except Exception:
        pass

    cmd = codepilot_command(
        "daemon",
        "--foreground",
        "--no-ui",
        "--interval",
        str(interval),
        "--max-concurrent",
        str(max_concurrent),
        "--shell",
        shell,
        "--executor",
        executor,
    )
    if project:
        cmd.extend(["--project", project])
    if not auto_commit:
        cmd.append("--no-auto-commit")

    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fp,
        "stderr": log_fp,
        "close_fds": True,
    }
    if getattr(sys, "frozen", False):
        child_env = os.environ.copy()
        child_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        popen_kwargs["env"] = child_env
    if os.name == "nt":
        popen_kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        popen_kwargs["startupinfo"] = hidden_windows_startupinfo()
    else:
        popen_kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **popen_kwargs)


def start_daemon_service(
    *,
    project: str | None = None,
    interval: int = 60,
    max_concurrent: int = 1,
    shell: str = "auto",
    executor: str = "auto",
    auto_commit: bool = True,
) -> dict:
    if not project:
        raise RuntimeError("启动 daemon 必须指定项目。")
    existing = daemon_service_status(project)
    if existing["running"]:
        existing["started"] = False
        return existing

    _cleanup_service_files(project)
    proc = _spawn_detached_daemon(
        project=project,
        interval=interval,
        max_concurrent=max_concurrent,
        shell=shell,
        executor=executor,
        auto_commit=auto_commit,
    )
    deadline = time.monotonic() + 15.0
    last_status = daemon_service_status(project)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = ""
            log_file = _service_log_path(project)
            try:
                tail = log_file.read_text(encoding="utf-8", errors="replace")[-1500:]
            except Exception:
                pass
            raise RuntimeError(f"daemon 启动后立即退出（exit={proc.returncode}）\n{tail}")
        last_status = daemon_service_status(project)
        if last_status["running"]:
            last_status["started"] = True
            return last_status
        time.sleep(0.2)

    tail = ""
    log_file = _service_log_path(project)
    try:
        tail = log_file.read_text(encoding="utf-8", errors="replace")[-1500:]
    except Exception:
        pass
    raise RuntimeError(f"daemon 启动请求已发出，但 15s 内未进入运行状态。\n{tail}")


def request_daemon_service_start(
    project: str,
    *,
    wait_seconds: float = 10.0,
) -> dict:
    """Request daemon startup from a short-lived external CLI process.

    Web UI uses this path so the daemon is not created by the Web UI server
    process. `codepilot ui restart` can still kill the Web UI process tree
    without also killing an independently launched project daemon.
    """
    if not project:
        raise RuntimeError("启动 daemon 必须指定项目。")
    existing = daemon_service_status(project)
    if existing["running"]:
        existing["started"] = False
        return existing

    log_file = _service_log_path(project)
    append_log_header(log_file, f"\n--- request-start {_now_iso()} project={project} via cli ---\n")
    cmd = codepilot_command("daemon", "--project", project)
    launcher_pid = _spawn_detached_command_via_launcher(cmd, log_file=log_file)

    deadline = time.monotonic() + max(float(wait_seconds), 0.0)
    last_status = daemon_service_status(project)
    while time.monotonic() < deadline:
        last_status = daemon_service_status(project)
        if last_status["running"]:
            last_status["started"] = True
            last_status["launcher_pid"] = launcher_pid
            return last_status
        time.sleep(0.2)

    last_status["started"] = False
    last_status["launcher_pid"] = launcher_pid
    raise RuntimeError(f"daemon 启动请求已发出，但 {wait_seconds:g}s 内未进入运行状态。")


def stop_daemon_service(project: str | None = None) -> dict:
    if not project:
        raise RuntimeError("停止 daemon 必须指定项目。")
    targets = _service_targets(project)
    if not targets:
        _cleanup_service_files(project)
        return {"stopped": False, "stop_requested": False, "pids": []}
    if targets:
        db.upsert_service_state(
            "daemon",
            _service_scope(project),
            pid=int(targets[0]),
            status="stopping",
            log_path=str(_service_log_path(project)),
            heartbeat_at=_now_iso(),
            meta={"project": project or "", "pid": int(targets[0]), "stop_requested_at": _now_iso()},
        )
    return {"stopped": False, "stop_requested": True, "pids": targets}


def _stop_requested(project: str | None = None) -> bool:
    try:
        db._invalidate_service_state_caches()
    except Exception:
        pass
    state = db.get_service_state("daemon", _service_scope(project))
    if not state:
        return False
    return str(state.get("status") or "").strip().lower() == "stopping"


def _sleep_or_stop(project: str | None, seconds: int) -> bool:
    deadline = time.monotonic() + max(0, int(seconds))
    while time.monotonic() < deadline:
        if _stop_requested(project):
            return True
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return _stop_requested(project)


def _tick_heartbeat(project: str | None = None) -> None:
    """Refresh daemon heartbeat in ``service_states``."""
    try:
        db._invalidate_service_state_caches()
    except Exception:
        pass
    state = db.get_service_state("daemon", _service_scope(project))
    current_status = str((state or {}).get("status") or "").strip().lower()
    status = "stopping" if current_status == "stopping" else "running"
    try:
        db.touch_service_state(
            "daemon",
            _service_scope(project),
            pid=os.getpid(),
            log_path=str(_service_log_path(project)),
            status=status,
        )
    except Exception:
        logger.debug("touch_service_state failed for daemon heartbeat", exc_info=True)


def _start_heartbeat_thread(project: str | None = None, *, interval_seconds: int = 10) -> threading.Event:
    """Keep the service heartbeat fresh while a loop iteration is busy.

    A daemon iteration can spend minutes inside ``run_backlog`` waiting for a
    builder/reviewer subprocess. The main loop heartbeat alone would look
    stale during that wait, so a tiny companion thread reports process liveness.
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(max(1, int(interval_seconds))):
            _tick_heartbeat(project)

    thread = threading.Thread(target=_beat, name="codepilot-daemon-heartbeat", daemon=True)
    thread.start()
    return stop


def _ensure_ui_service_process(port: int = 8766, *, project: str | None = None) -> bool:
    """Ensure Web UI runs in a **separate process** from daemon.

    Historically foreground daemon started Web UI as an in-process thread.
    That coupled lifecycles and made isolation harder. We now shell out to
    `codepilot ui start --no-daemon` so UI and workflow stay decoupled.
    """
    cmd = codepilot_command(
        "ui",
        "start",
        "--no-open",
        "--no-daemon",
        "--port",
        str(int(port)),
    )
    if project:
        cmd.extend(["--project", project])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            timeout=30,
        )
    except Exception as exc:
        echo(f"[yellow]Web UI 独立进程启动失败：{safe(exc)}[/yellow]")
        return False

    if result.returncode != 0:
        detail = (decode_subprocess_text(result.stderr) or decode_subprocess_text(result.stdout)).strip()
        if detail:
            echo(f"[yellow]Web UI 独立进程启动失败：{detail[-800:]}[/yellow]")
        else:
            echo(f"[yellow]Web UI 独立进程启动失败（exit={result.returncode}）[/yellow]")
        return False

    echo(f"[cyan]Web UI[/cyan] 已作为独立进程运行  http://127.0.0.1:{port}/")
    return True


def _acquire_lock(project: str | None = None) -> bool:
    scope = _service_scope(project)
    state = db.get_service_state("daemon", scope)
    current_pid = os.getpid()
    if state:
        try:
            pid = int(state.get("pid") or 0)
        except Exception:
            pid = 0
        if pid and pid != current_pid and is_process_alive(pid) and not _state_heartbeat_stale(state):
            return False
    db.upsert_service_state(
        "daemon",
        scope,
        pid=current_pid,
        status="running",
        log_path=str(_service_log_path(project)),
        heartbeat_at=_now_iso(),
        meta={"project": project or "", "started_at": _now_iso(), "executor": "foreground"},
    )
    return True


def _release_lock(project: str | None = None) -> None:
    db.clear_service_state("daemon", _service_scope(project))


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
@click.option("--ui/--no-ui", "enable_ui", default=True, help="前台模式下同时确保 Web UI 独立进程运行（默认开启）")
@click.option("--ui-port", type=int, default=8766, help="Web UI 端口")
@click.option("--foreground", is_flag=True, help="以前台模式运行（用于调试）")
@click.option("--status", "show_status", is_flag=True, help="查看后台 daemon 状态")
@click.option("--stop", "stop_service", is_flag=True, help="停止后台 daemon")
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
    foreground: bool,
    show_status: bool,
    stop_service: bool,
):
    """后台启动 daemon，持续轮询 backlog 并执行任务."""
    db.init_db()
    if show_status:
        if not project:
            raise click.ClickException("查看 daemon 状态必须指定 --project。")
        status = daemon_service_status(project)
        if status["running"]:
            echo(f"[green]Daemon 运行中[/green]  PID={status['pid']}  项目={status['project'] or 'all'}")
            echo(f"[dim]日志: {status['log']}[/dim]")
        else:
            echo("[dim]Daemon 未在运行[/dim]")
        return

    if stop_service:
        if not project:
            raise click.ClickException("停止 daemon 必须指定 --project。")
        try:
            result = stop_daemon_service(project)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if result.get("stop_requested"):
            echo(f"[green]Daemon 已请求停止轮询[/green]  PID={','.join(str(pid) for pid in result['pids'])}")
            echo("[dim]当前正在执行的任务不会被中断；完成后不再领取下一个任务。[/dim]")
        elif result["stopped"]:
            echo(f"[green]Daemon 已停止[/green]  PID={','.join(str(pid) for pid in result['pids'])}")
        else:
            echo("[dim]Daemon 未在运行[/dim]")
        return

    if not foreground:
        if not project:
            raise click.ClickException("启动 daemon 必须指定 --project。")
        try:
            result = start_daemon_service(
                project=project,
                interval=interval,
                max_concurrent=max_concurrent,
                shell=shell,
                executor=executor,
                auto_commit=auto_commit,
            )
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if result["started"]:
            echo(f"[green]Daemon 已后台启动[/green]  PID={result['pid']}")
        else:
            echo(f"[yellow]Daemon 已在运行[/yellow]  PID={result['pid']}")
        echo(f"[dim]日志: {result['log']}[/dim]")
        return

    if not project:
        raise click.ClickException("前台运行 daemon 必须指定 --project。")

    if not _acquire_lock(project):
        echo("[red]已有 daemon 实例运行中，退出[/red]")
        return

    try:
        if enable_ui:
            _ensure_ui_service_process(ui_port, project=project)
        _ensure_feishu_service(project)
        _run_loop(project, interval, verbose, shell, executor, auto_commit, max_concurrent)
    except KeyboardInterrupt:
        echo()
        echo("[yellow]守护进程收到停止信号，退出[/yellow]")
    finally:
        _release_lock(project)


def _ensure_feishu_service(project: str | None = None) -> None:
    project_info = None
    if project:
        try:
            project_info = db.get_project(project)
        except Exception as exc:
            echo(f"[yellow]飞书项目配置读取失败：{safe(exc)}[/yellow]")
            return
        if not project_info:
            echo(f"[yellow]项目 {project} 未注册，跳过飞书服务自动启动。[/yellow]")
            return
    try:
        result = ensure_service_running_if_enabled(project_info) if project_info else ensure_service_running_if_enabled()
    except Exception as exc:
        echo(f"[yellow]飞书服务自动启动失败：{safe(exc)}[/yellow]")
        return
    if result.get("error"):
        echo(f"[yellow]飞书服务自动启动失败：{result['error']}[/yellow]")
        return
    if result.get("started"):
        echo(f"[green]飞书服务已后台启动[/green]  PID={result.get('pid')}")


def _run_agent_daemon_tick(project: str | None, *, verbose: bool = False) -> None:
    from codepilot.scheduled.daemon import run_project_agent_jobs

    projects = [db.get_project(project)] if project else db.list_projects()
    for project_info in [item for item in projects if item]:
        try:
            result = run_project_agent_jobs(project_info)
        except Exception as exc:
            echo(
                f"[yellow]scheduled/event agent 调度失败 "
                f"{safe(project_info.get('name') or '')}：{safe(exc)}[/yellow]"
            )
            continue
        if verbose:
            scheduled_count = int(result.get("scheduled", {}).get("job_count") or 0)
            event_count = int(result.get("events", {}).get("job_count") or 0)
            if scheduled_count or event_count:
                echo(
                    f"[dim]{project_info.get('name')}: "
                    f"scheduled_agents={scheduled_count} event_agents={event_count}[/dim]"
                )


def _run_loop(
    project: str | None,
    interval: int,
    verbose: bool,
    shell: str,
    executor: str,
    auto_commit: bool,
    max_concurrent: int = 1,
) -> None:
    from codepilot.core.cli_progress import maybe_cli_renderer

    echo(
        f"[cyan]CodePilot Daemon[/cyan]  项目: {project or 'all'}  间隔: {interval}s  "
        f"执行器: {executor}  并行度: {max_concurrent}\n"
    )
    echo("[yellow]守护进程运行中，按 Ctrl+C 停止[/yellow]")
    click.echo()

    heartbeat_stop = _start_heartbeat_thread(project)

    try:
        with maybe_cli_renderer():
            while True:
                _tick_heartbeat(project)
                if _stop_requested(project):
                    echo("[yellow]收到停止轮询请求，退出 daemon；未中断正在执行的任务。[/yellow]")
                    break
                reaped = reap_stalled_tasks(project)
                for task in reaped:
                    echo(f"[yellow]已回收卡住任务 #{task['id']}：{task['title']}[/yellow]")

                _run_agent_daemon_tick(project, verbose=verbose)

                stats = _get_combined_stats(project)
                if stats["backlog"] == 0:
                    if verbose:
                        echo("[dim]空闲，backlog: 0[/dim]")
                    if _sleep_or_stop(project, interval):
                        echo("[yellow]收到停止轮询请求，退出 daemon。[/yellow]")
                        break
                    continue

                echo(
                    f"[green]backlog: {stats['backlog']}[/green]  "
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
                            f"[dim]{name}: processed={result['processed']} "
                            f"done={result['done']} failed={result['failed']} "
                            f"requeued={result['requeued']}[/dim]"
                        )
                if _sleep_or_stop(project, interval):
                    echo("[yellow]收到停止轮询请求，退出 daemon。[/yellow]")
                    break
    finally:
        heartbeat_stop.set()


def _get_combined_stats(project: str | None) -> dict:
    if project:
        return db.get_task_stats(project)

    combined = {"backlog": 0, "in_progress": 0, "done": 0, "failed": 0, "cancelled": 0, "total": 0}
    for proj in db.list_projects():
        stats = db.get_task_stats(proj["name"])
        for key in combined:
            combined[key] += stats.get(key, 0)
    return combined
