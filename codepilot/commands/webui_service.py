"""Persistent Web UI service — start / stop / restart / status.

Spawns a fully-detached child process running `codepilot ui` so the server
keeps running after the launching terminal is closed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import click

from codepilot.output import echo, safe
from codepilot.runtime import is_process_alive


STATE_DIR = Path.home() / ".codepilot"
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
    pid = _read_pid()
    if pid and is_process_alive(pid):
        return pid
    return None


def _cleanup_files() -> None:
    for p in (PID_FILE, META_FILE):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


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


def _send_stop(pid: int) -> None:
    """Best-effort stop: SIGTERM, fall back to harder kill if needed."""
    try:
        if os.name == "nt":
            # On Windows, os.kill with SIGTERM maps to TerminateProcess.
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception as exc:
        echo(f"[yellow]发送停止信号失败：{safe(exc)}[/yellow]")

    for _ in range(50):  # up to 5s
        if not is_process_alive(pid):
            return
        time.sleep(0.1)

    # Still alive — force kill
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        echo(f"[yellow]强制结束失败：{safe(exc)}[/yellow]")


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
    pid = _read_pid()
    if not pid:
        echo("[dim]Web UI 未在运行[/dim]")
        _cleanup_files()
        return
    if not is_process_alive(pid):
        echo(f"[dim]Web UI 已不存在（PID={pid} 已退出），清理状态文件[/dim]")
        _cleanup_files()
        return

    _send_stop(pid)

    if is_process_alive(pid):
        echo(f"[red]无法停止 PID={pid}[/red]")
        raise click.Abort()

    _cleanup_files()
    echo(f"[green]Web UI 已停止[/green]  (PID={pid})")


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

    pid = _read_pid()
    if pid and is_process_alive(pid):
        _send_stop(pid)
    _cleanup_files()
    time.sleep(0.3)
    ctx.invoke(start_cmd, host=resolved_host, port=resolved_port, open_browser=open_browser)


@webui.command("status")
def status_cmd() -> None:
    """查看 Web UI 服务运行状态。"""
    pid = _read_pid()
    if not pid:
        echo("[dim]Web UI 未在运行[/dim]")
        return
    if not is_process_alive(pid):
        echo(f"[yellow]PID 文件记录 PID={pid}，但该进程已不存在。可执行 codepilot webui stop 清理状态。[/yellow]")
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
