"""Persistent Web UI service — start / stop / restart / status.

Spawns a fully-detached child process running `codepilot ui` so the server
keeps running after the launching terminal is closed.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import click

from codepilot.storage import database as db
from codepilot.commands.daemon import request_daemon_service_start
from codepilot.commands.feishu import ensure_service_running_if_enabled
from codepilot.core.output import echo, safe
from codepilot.core.paths import global_storage_root
from codepilot.core.runtime import is_process_alive, stop_process_tree
from codepilot.core.text_decode import decode_subprocess_text


STATE_DIR = global_storage_root() / "webui"
LOG_FILE = STATE_DIR / "webui.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
WEBUI_HOST_ENV = "CODEPILOT_WEBUI_HOST"
WEBUI_PORT_ENV = "CODEPILOT_WEBUI_PORT"


def _service_scope() -> str:
    return "_global"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _env_host() -> str | None:
    raw = os.environ.get(WEBUI_HOST_ENV, "").strip()
    return raw or None


def _env_port() -> int | None:
    raw = os.environ.get(WEBUI_PORT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise click.ClickException(f"{WEBUI_PORT_ENV} 必须是整数端口：{raw}") from exc
    if value <= 0 or value > 65535:
        raise click.ClickException(f"{WEBUI_PORT_ENV} 超出有效端口范围：{value}")
    return value


def _default_host() -> str:
    return _env_host() or DEFAULT_HOST


def _default_port() -> int:
    return _env_port() or DEFAULT_PORT


def _resolve_start_host_port(host: str | None, port: int | None) -> tuple[str, int]:
    return host or _default_host(), port if port is not None else _default_port()


def _resolve_restart_host_port(host: str | None, port: int | None, meta: dict) -> tuple[str, int]:
    resolved_host = host or _env_host() or meta.get("host") or DEFAULT_HOST
    resolved_port = port if port is not None else (_env_port() or int(meta.get("port") or DEFAULT_PORT))
    return str(resolved_host), int(resolved_port)


def _read_meta() -> dict:
    state = db.get_service_state("webui", _service_scope())
    if state and isinstance(state.get("meta"), dict) and state.get("meta"):
        return dict(state["meta"])
    return {}


def _write_meta(pid: int, host: str, port: int) -> None:
    payload = {"pid": pid, "host": host, "port": port, "started_at": _now_iso()}
    db.upsert_service_state(
        "webui",
        _service_scope(),
        pid=int(pid),
        status="running",
        log_path=str(LOG_FILE),
        heartbeat_at=_now_iso(),
        meta=payload,
    )


def _read_pid() -> int | None:
    state = db.get_service_state("webui", _service_scope())
    if state and state.get("pid"):
        try:
            return int(state["pid"])
        except Exception:
            pass
    return None


def _alive_pid() -> int | None:
    """Return the live PID of the running service, or None if not running."""
    targets = _service_targets()
    if not targets:
        return None
    listener_pids = _listening_service_pids()
    live_pid = listener_pids[0] if listener_pids else targets[0]
    _sync_state_pid(live_pid)
    return live_pid


def _cleanup_files() -> None:
    db.clear_service_state("webui", _service_scope())


def _service_host_port() -> tuple[str, int]:
    meta = _read_meta()
    host = str(meta.get("host") or _default_host())
    try:
        port = int(meta.get("port") or _default_port())
    except Exception:
        port = _default_port()
    return host, port


def _sync_state_pid(pid: int) -> None:
    meta = _read_meta()
    meta["pid"] = int(pid)
    if "host" not in meta:
        meta["host"] = _default_host()
    if "port" not in meta:
        meta["port"] = _default_port()
    if "started_at" not in meta:
        meta["started_at"] = _now_iso()
    db.upsert_service_state(
        "webui",
        _service_scope(),
        pid=int(pid),
        status="running",
        log_path=str(LOG_FILE),
        heartbeat_at=_now_iso(),
        meta=meta,
    )


def _listening_service_pids() -> list[int]:
    host, port = _service_host_port()
    if platform.system().lower() != "windows":
        return []

    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"Get-NetTCPConnection -State Listen -LocalPort {int(port)} | "
        "Select-Object LocalAddress,OwningProcess | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", script],
            capture_output=True,
            text=False,
            timeout=15,
        )
    except Exception:
        return []

    raw = decode_subprocess_text(result.stdout).strip()
    if result.returncode != 0 or not raw:
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []

    rows = payload if isinstance(payload, list) else [payload]
    pids: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        local_address = str(row.get("LocalAddress") or "")
        if host not in {"0.0.0.0", "::", ""} and local_address not in {host, "0.0.0.0", "::"}:
            continue
        try:
            pid = int(row.get("OwningProcess"))
        except Exception:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _service_targets() -> list[int]:
    targets: list[int] = []
    pid = _read_pid()
    if pid and is_process_alive(pid):
        targets.append(int(pid))
    for listener_pid in _listening_service_pids():
        if is_process_alive(listener_pid) and listener_pid not in targets:
            targets.append(listener_pid)
    return targets


def _remaining_service_pids(targets: list[int], *, wait_seconds: float = 2.0) -> list[int]:
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        remaining: list[int] = []
        for pid in targets:
            if is_process_alive(pid) and pid not in remaining:
                remaining.append(pid)
        for pid in _listening_service_pids():
            if is_process_alive(pid) and pid not in remaining:
                remaining.append(pid)
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.2)


def _foreground_ui_command(host: str, port: int) -> list[str]:
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.extend(["-m", "codepilot"])
    command.extend(["ui", "--host", host, "--port", str(port), "--no-open"])
    return command


def _spawn_detached(host: str, port: int) -> subprocess.Popen:
    """Spawn `codepilot ui --host ... --port ... --no-open` as a detached process.

    Output goes to LOG_FILE so the terminal isn't blocked, and the process
    survives its parent.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_fp = open(LOG_FILE, "ab")
    try:
        log_fp.write(f"\n--- start {_now_iso()} host={host} port={port} ---\n".encode("utf-8"))
        log_fp.flush()
    except Exception:
        pass

    cmd = _foreground_ui_command(host, port)

    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fp,
        "stderr": log_fp,
        "close_fds": True,
    }

    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — child has no console
        # and does not die when the launching terminal closes.
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        # POSIX: new session detaches from the controlling terminal.
        popen_kwargs["start_new_session"] = True

    return subprocess.Popen(cmd, **popen_kwargs)


def _send_stop(pid: int) -> bool:
    """Stop the detached Web UI process tree."""
    try:
        return stop_process_tree(pid, wait_seconds=5)
    except Exception as exc:
        echo(f"[yellow]停止 Web UI 失败：{safe(exc)}[/yellow]")
        return False


@click.group("webui")
def webui():
    """Web UI 后台服务管理（start / stop / restart / status）。"""


@webui.command("start")
@click.option("--host", default=None, help=f"监听地址，默认读取 {WEBUI_HOST_ENV} 或 {DEFAULT_HOST}")
@click.option("--port", type=int, default=None, help=f"监听端口，默认读取 {WEBUI_PORT_ENV} 或 {DEFAULT_PORT}")
@click.option("--open/--no-open", "open_browser", default=True, show_default=True, help="启动后在浏览器打开")
@click.option("--daemon/--no-daemon", "start_daemon", default=True, show_default=True, help="同时确保 daemon 后台运行")
@click.option("--project", "-p", default="", help="同时启动指定项目的任务执行服务")
def start_cmd(host: str | None, port: int | None, open_browser: bool, start_daemon: bool, project: str) -> None:
    """启动 Web UI 后台服务（即使关掉终端也保持运行）。"""
    resolved_host, resolved_port = _resolve_start_host_port(host, port)
    existing = _alive_pid()
    if existing:
        meta = _read_meta()
        url = f"http://{meta.get('host', resolved_host)}:{meta.get('port', resolved_port)}/"
        echo(f"[yellow]Web UI 已在运行（PID={existing}） {url}[/yellow]")
        echo("[dim]如果需要重启：codepilot ui restart[/dim]")
        if start_daemon:
            _ensure_daemon_started(project)
        _ensure_feishu_started()
        return

    # Clean up stale state
    _cleanup_files()

    try:
        proc = _spawn_detached(resolved_host, resolved_port)
    except Exception as exc:
        echo(f"[red]启动失败：{safe(exc)}[/red]")
        raise click.Abort()

    # Verify it stays alive briefly
    time.sleep(0.8)
    if proc.poll() is not None:
        tail = ""
        try:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-1500:]
        except Exception:
            pass
        echo(f"[red]Web UI 启动后立即退出（exit={proc.returncode}）[/red]")
        if tail:
            echo(f"[dim]--- 日志尾部 ---\n{tail}[/dim]")
        raise click.Abort()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _write_meta(proc.pid, resolved_host, resolved_port)

    url = f"http://{resolved_host}:{resolved_port}/"
    echo(f"[green]Web UI 已启动[/green]  PID={proc.pid}  {url}")
    echo(f"[dim]日志: {LOG_FILE}[/dim]")
    echo("[dim]停止: codepilot ui stop[/dim]")

    if start_daemon:
        _ensure_daemon_started(project)
    _ensure_feishu_started()

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass


def _ensure_daemon_started(project: str = "") -> None:
    if not project:
        echo("[dim]未指定 --project，跳过任务执行服务自动启动；可在 Web UI 项目页单独启动。[/dim]")
        return
    try:
        result = request_daemon_service_start(project)
    except Exception as exc:
        echo(f"[yellow]Daemon 后台启动失败：{safe(exc)}[/yellow]")
        return
    if result.get("started"):
        echo(f"[green]项目 {project} 任务执行服务已后台启动[/green]  PID={result.get('pid')}")
    else:
        echo(f"[dim]项目 {project} 任务执行服务已在运行[/dim]  PID={result.get('pid')}")


def _ensure_feishu_started() -> None:
    try:
        result = ensure_service_running_if_enabled()
    except Exception as exc:
        echo(f"[yellow]飞书服务自动启动失败：{safe(exc)}[/yellow]")
        return
    if result.get("error"):
        echo(f"[yellow]飞书服务自动启动失败：{result['error']}[/yellow]")
        return
    if result.get("started"):
        echo(f"[green]飞书服务已后台启动[/green]  PID={result.get('pid')}")


@webui.command("stop")
def stop_cmd() -> None:
    """停止后台 Web UI 服务。"""
    raw_pid = _read_pid()
    targets = _service_targets()
    if not raw_pid and not targets:
        echo("[dim]Web UI 未在运行[/dim]")
        _cleanup_files()
        return
    if not targets:
        echo(f"[dim]Web UI 已不存在（PID={raw_pid} 已退出），清理状态文件[/dim]")
        _cleanup_files()
        return

    try:
        state_meta = _read_meta()
        db.upsert_service_state(
            "webui",
            _service_scope(),
            pid=int(targets[0]),
            status="stopping",
            log_path=str(LOG_FILE),
            heartbeat_at=_now_iso(),
            meta=state_meta,
        )
    except Exception:
        pass

    failures: list[int] = []
    for pid in targets:
        if is_process_alive(pid) and not _send_stop(pid):
            failures.append(pid)

    survivors = _remaining_service_pids(targets)
    if survivors:
        echo(f"[red]无法停止 PID={','.join(str(pid) for pid in survivors)}[/red]")
        raise click.Abort()
    if failures:
        echo(f"[yellow]以下 PID 停止时返回失败，但进程已退出：{','.join(str(pid) for pid in failures)}[/yellow]")

    _cleanup_files()
    stopped = ",".join(str(pid) for pid in targets)
    echo(f"[green]Web UI 已停止[/green]  (PID={stopped})")


@webui.command("restart")
@click.option("--host", default=None, help="监听地址（默认沿用上次）")
@click.option("--port", type=int, default=None, help="监听端口（默认沿用上次）")
@click.option("--open/--no-open", "open_browser", default=False, show_default=True, help="重启后在浏览器打开")
@click.option("--daemon/--no-daemon", "start_daemon", default=True, show_default=True, help="同时确保 daemon 后台运行")
@click.option("--project", "-p", default="", help="同时启动指定项目的任务执行服务")
@click.pass_context
def restart_cmd(ctx: click.Context, host: str | None, port: int | None, open_browser: bool, start_daemon: bool, project: str) -> None:
    """重启 Web UI 服务（沿用上次的 host/port，也可通过选项覆盖）。"""
    meta = _read_meta()
    resolved_host, resolved_port = _resolve_restart_host_port(host, port, meta)

    targets = _service_targets()
    failures: list[int] = []
    for pid in targets:
        if is_process_alive(pid) and not _send_stop(pid):
            failures.append(pid)
    survivors = _remaining_service_pids(targets)
    if survivors:
        echo(f"[red]无法停止 PID={','.join(str(pid) for pid in survivors)}[/red]")
        raise click.Abort()
    if failures:
        echo(f"[yellow]以下 PID 停止时返回失败，但进程已退出：{','.join(str(pid) for pid in failures)}[/yellow]")
    _cleanup_files()
    time.sleep(0.3)
    ctx.invoke(start_cmd, host=resolved_host, port=resolved_port, open_browser=open_browser, start_daemon=start_daemon, project=project)


@webui.command("status")
def status_cmd() -> None:
    """查看 Web UI 服务运行状态。"""
    pid = _alive_pid()
    if not pid:
        echo("[dim]Web UI 未在运行[/dim]")
        return
    meta = _read_meta()
    url = f"http://{meta.get('host', DEFAULT_HOST)}:{meta.get('port', DEFAULT_PORT)}/"
    echo(f"[green]Web UI 运行中[/green]  PID={pid}  {url}")
    started = meta.get("started_at")
    if started:
        echo(f"[dim]启动时间: {started}[/dim]")
    echo(f"[dim]日志: {LOG_FILE}[/dim]")


@webui.command("logs")
@click.option("--tail", type=int, default=50, show_default=True, help="展示末尾多少行")
def logs_cmd(tail: int) -> None:
    """查看 Web UI 后台日志。"""
    if not LOG_FILE.exists():
        echo("[dim]暂无日志[/dim]")
        return
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        echo(f"[red]读取日志失败：{safe(exc)}[/red]")
        return
    lines = text.splitlines()
    if tail > 0 and len(lines) > tail:
        lines = lines[-tail:]
    click.echo("\n".join(lines))
