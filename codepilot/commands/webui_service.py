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

from codepilot.output import echo, safe
from codepilot.paths import global_storage_root
from codepilot.runtime import is_process_alive, stop_process_tree


STATE_DIR = global_storage_root() / "webui"
PID_FILE = STATE_DIR / "webui.pid"
META_FILE = STATE_DIR / "webui.json"
LOG_FILE = STATE_DIR / "webui.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_meta() -> dict:
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(pid: int, host: str, port: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(
        json.dumps(
            {"pid": pid, "host": host, "port": port, "started_at": _now_iso()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
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
    for p in (PID_FILE, META_FILE):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _service_host_port() -> tuple[str, int]:
    meta = _read_meta()
    host = str(meta.get("host") or DEFAULT_HOST)
    try:
        port = int(meta.get("port") or DEFAULT_PORT)
    except Exception:
        port = DEFAULT_PORT
    return host, port


def _sync_state_pid(pid: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(int(pid)), encoding="utf-8")
    meta = _read_meta()
    meta["pid"] = int(pid)
    if "host" not in meta:
        meta["host"] = DEFAULT_HOST
    if "port" not in meta:
        meta["port"] = DEFAULT_PORT
    if "started_at" not in meta:
        meta["started_at"] = _now_iso()
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


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
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return []

    raw = (result.stdout or "").strip()
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

    cmd = [
        sys.executable, "-m", "codepilot", "ui",
        "--host", host, "--port", str(port), "--no-open",
    ]

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
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="监听地址")
@click.option("--port", type=int, default=DEFAULT_PORT, show_default=True, help="监听端口")
@click.option("--open/--no-open", "open_browser", default=True, show_default=True, help="启动后在浏览器打开")
def start_cmd(host: str, port: int, open_browser: bool) -> None:
    """启动 Web UI 后台服务（即使关掉终端也保持运行）。"""
    existing = _alive_pid()
    if existing:
        meta = _read_meta()
        url = f"http://{meta.get('host', host)}:{meta.get('port', port)}/"
        echo(f"[yellow]Web UI 已在运行（PID={existing}） {url}[/yellow]")
        echo("[dim]如果需要重启：codepilot webui restart[/dim]")
        return

    # Clean up stale state
    _cleanup_files()

    try:
        proc = _spawn_detached(host, port)
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
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    _write_meta(proc.pid, host, port)

    url = f"http://{host}:{port}/"
    echo(f"[green]Web UI 已启动[/green]  PID={proc.pid}  {url}")
    echo(f"[dim]日志: {LOG_FILE}[/dim]")
    echo("[dim]停止: codepilot webui stop[/dim]")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass


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
@click.pass_context
def restart_cmd(ctx: click.Context, host: str | None, port: int | None, open_browser: bool) -> None:
    """重启 Web UI 服务（沿用上次的 host/port，也可通过选项覆盖）。"""
    meta = _read_meta()
    resolved_host = host or meta.get("host") or DEFAULT_HOST
    resolved_port = port if port is not None else int(meta.get("port") or DEFAULT_PORT)

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
    ctx.invoke(start_cmd, host=resolved_host, port=resolved_port, open_browser=open_browser)


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
