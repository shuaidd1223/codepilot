"""Feishu long-connection bot service."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click

from codepilot.core.output import echo, safe
from codepilot.core.paths import global_storage_root
from codepilot.core.runtime import is_process_alive, stop_process_tree
from codepilot.core.service_launcher import DetachedProcessHandle, append_log_header, spawn_detached_command_via_launcher
from codepilot.feishu_bot import handle_event_payload, load_feishu_bot_config, validate_feishu_bot_config
from codepilot.storage import database as db


STATE_DIR = global_storage_root() / "feishu"
LOG_FILE = STATE_DIR / "feishu.log"
_HEARTBEAT_SECONDS = 5
_RESTART_BACKOFF_SECONDS = (2, 5, 10, 20, 30)


def _service_scope() -> str:
    return "_global"


def _handle_event_error_reply(message: str) -> dict[str, Any]:
    text = f"CodePilot 飞书处理失败：{str(message or '').strip()}"
    return {
        "type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "CodePilot 飞书处理失败"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": text},
                }
            ],
        },
    }


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _worker_script() -> Path:
    return Path(__file__).resolve().parents[1] / "feishu_worker.mjs"


def _read_meta() -> dict:
    state = db.get_service_state("feishu", _service_scope())
    if state and isinstance(state.get("meta"), dict) and state.get("meta"):
        return dict(state["meta"])
    return {}


def _service_status() -> dict:
    state = db.get_service_state("feishu", _service_scope())
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    try:
        pid = int(state.get("pid") or 0) if state else 0
    except Exception:
        pid = 0
    running = bool(pid and is_process_alive(pid))
    return {
        "running": running,
        "pid": pid if running else 0,
        "started_at": meta.get("started_at") or "",
        "worker_pid": int(meta.get("worker_pid") or 0) if running else 0,
        "restart_count": int(meta.get("restart_count") or 0),
        "last_exit_code": meta.get("last_exit_code"),
        "last_restart_at": meta.get("last_restart_at") or "",
        "log": str(state.get("log_path") or LOG_FILE) if state else str(LOG_FILE),
    }


def _write_state(pid: int, *, status: str = "running", meta_updates: dict[str, Any] | None = None) -> None:
    meta = _read_meta()
    meta.update(
        {
            "pid": int(pid),
            "started_at": meta.get("started_at") or _now_iso(),
        }
    )
    if meta_updates:
        meta.update(meta_updates)
    db.upsert_service_state(
        "feishu",
        _service_scope(),
        pid=int(pid),
        status=status,
        log_path=str(LOG_FILE),
        heartbeat_at=_now_iso(),
        meta=meta,
    )


def _touch_state(pid: int, *, status: str = "running", meta_updates: dict[str, Any] | None = None) -> None:
    current = _read_meta()
    if meta_updates:
        current.update(meta_updates)
    db.upsert_service_state(
        "feishu",
        _service_scope(),
        pid=int(pid),
        log_path=str(LOG_FILE),
        status=status,
        heartbeat_at=_now_iso(),
        meta=current,
    )


def _stop_requested() -> bool:
    state = db.get_service_state("feishu", _service_scope())
    if not state:
        return False
    return str(state.get("status") or "").strip().lower() == "stopping"


def _mark_stopping(pid: int) -> None:
    db.touch_service_state(
        "feishu",
        _service_scope(),
        pid=int(pid),
        log_path=str(LOG_FILE),
        status="stopping",
    )


def _clear_state() -> None:
    db.clear_service_state("feishu", _service_scope())


def _build_worker_env() -> dict[str, str]:
    cfg = load_feishu_bot_config()
    env = os.environ.copy()
    env.update(
        {
            "CODEPILOT_FEISHU_APP_ID": cfg.app_id,
            "CODEPILOT_FEISHU_APP_SECRET": cfg.app_secret,
            "CODEPILOT_FEISHU_PYTHON": sys.executable,
        }
    )
    return env


def _worker_command() -> list[str]:
    cfg = load_feishu_bot_config()
    return [cfg.node_command or "node", str(_worker_script())]


def _check_runtime_ready() -> None:
    cfg = load_feishu_bot_config()
    problems = validate_feishu_bot_config()
    if problems:
        raise RuntimeError("飞书机器人配置不完整：\n- " + "\n- ".join(problems))

    repo_root = _repo_root()
    package_json = repo_root / "package.json"
    if not package_json.exists():
        raise RuntimeError(f"缺少 {package_json.name}，请先在仓库根目录安装飞书 SDK 依赖。")

    try:
        subprocess.run(
            [cfg.node_command or "node", "-e", "require.resolve('@larksuiteoapi/node-sdk')"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=False,
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(
            "未找到 @larksuiteoapi/node-sdk。请在仓库根目录执行 `npm install`。"
        ) from exc


def _spawn_detached() -> DetachedProcessHandle:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    append_log_header(LOG_FILE, f"\n--- start {_now_iso()} ---\n")
    cmd = [sys.executable, "-m", "codepilot", "feishu", "run"]
    pid = spawn_detached_command_via_launcher(cmd, log_file=LOG_FILE, cwd=_repo_root())
    return DetachedProcessHandle(pid)


def _spawn_worker() -> subprocess.Popen:
    return subprocess.Popen(_worker_command(), cwd=str(_repo_root()), env=_build_worker_env())


def _supervise_worker() -> None:
    _check_runtime_ready()
    status = _service_status()
    if status["running"]:
        raise click.ClickException(f"飞书服务已在运行，PID={status['pid']}")

    db.init_db()
    _clear_state()
    _write_state(os.getpid(), meta_updates={"restart_count": 0, "worker_pid": 0, "last_exit_code": None})
    echo("[cyan]CodePilot Feishu Bot[/cyan] 长连接已启动，按 Ctrl+C 停止。")
    restart_count = 0
    proc: subprocess.Popen | None = None
    try:
        while True:
            if _stop_requested():
                break
            proc = _spawn_worker()
            _touch_state(
                os.getpid(),
                meta_updates={
                    "worker_pid": int(proc.pid),
                    "restart_count": int(restart_count),
                    "last_restart_at": _now_iso(),
                },
            )
            while True:
                code = proc.poll()
                if code is not None:
                    if _stop_requested():
                        return
                    restart_count += 1
                    _touch_state(
                        os.getpid(),
                        meta_updates={
                            "worker_pid": 0,
                            "restart_count": int(restart_count),
                            "last_exit_code": int(code),
                        },
                    )
                    backoff = _RESTART_BACKOFF_SECONDS[min(restart_count - 1, len(_RESTART_BACKOFF_SECONDS) - 1)]
                    echo(
                        f"[yellow]飞书 worker 已退出[/yellow]  exit={code}  "
                        f"{backoff}s 后自动重启（第 {restart_count} 次）"
                    )
                    time.sleep(backoff)
                    break
                _touch_state(
                    os.getpid(),
                    meta_updates={
                        "worker_pid": int(proc.pid),
                        "restart_count": int(restart_count),
                    },
                )
                time.sleep(_HEARTBEAT_SECONDS)
    except KeyboardInterrupt:
        echo()
        echo("[yellow]正在停止飞书服务…[/yellow]")
        _mark_stopping(os.getpid())
    finally:
        try:
            if proc is not None and proc.poll() is None:
                stop_process_tree(proc.pid, wait_seconds=5)
        except Exception:
            pass
        _clear_state()


def _start_foreground() -> None:
    _supervise_worker()


def ensure_service_running_if_enabled() -> dict[str, Any]:
    """Best-effort autostart hook for UI/daemon entrypoints."""
    cfg = load_feishu_bot_config()
    if not cfg.enabled:
        return {"enabled": False, "running": False, "started": False}

    try:
        _check_runtime_ready()
    except Exception as exc:
        return {"enabled": True, "running": False, "started": False, "error": str(exc)}

    status = _service_status()
    if status["running"]:
        return {"enabled": True, "running": True, "started": False, "pid": status["pid"]}

    _clear_state()
    proc = _spawn_detached()
    time.sleep(0.8)
    if proc.poll() is not None:
        tail = ""
        try:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-1500:]
        except Exception:
            pass
        return {
            "enabled": True,
            "running": False,
            "started": False,
            "error": f"飞书服务启动后立即退出（exit={proc.returncode}）\n{tail}",
        }
    return {"enabled": True, "running": True, "started": True, "pid": proc.pid, "log": str(LOG_FILE)}


@click.group("feishu", invoke_without_command=True)
@click.pass_context
def feishu_group(ctx: click.Context) -> None:
    """飞书企业应用长连接机器人。"""
    if ctx.invoked_subcommand is None:
        ctx.invoke(run_cmd)


@feishu_group.command("run")
def run_cmd() -> None:
    """以前台模式运行飞书长连接机器人。"""
    _start_foreground()


@feishu_group.command("start")
def start_cmd() -> None:
    """后台启动飞书长连接机器人。"""
    result = ensure_service_running_if_enabled()
    if not result.get("enabled"):
        raise click.ClickException("未启用飞书服务，请先把 [feishu_bot].enabled 设为 true。")
    if result.get("error"):
        raise click.ClickException(str(result["error"]))
    if result.get("started"):
        echo(f"[green]飞书服务已后台启动[/green]  PID={result['pid']}")
        echo(f"[dim]日志: {result['log']}[/dim]")
        echo("[dim]worker 异常退出后会自动重启。[/dim]")
        return
    echo(f"[yellow]飞书服务已在运行[/yellow]  PID={result['pid']}")
    echo(f"[dim]日志: {LOG_FILE}[/dim]")


@feishu_group.command("stop")
def stop_cmd() -> None:
    """停止后台飞书服务。"""
    status = _service_status()
    pid = int(status.get("pid") or 0)
    if not pid:
        _clear_state()
        echo("[dim]飞书服务未在运行[/dim]")
        return
    _mark_stopping(pid)
    if not stop_process_tree(pid, wait_seconds=5):
        raise click.ClickException(f"无法停止飞书服务 PID={pid}")
    _clear_state()
    echo(f"[green]飞书服务已停止[/green]  PID={pid}")


@feishu_group.command("status")
def status_cmd() -> None:
    """查看飞书服务状态。"""
    status = _service_status()
    if not status["running"]:
        echo("[dim]飞书服务未在运行[/dim]")
        return
    echo(f"[green]飞书服务运行中[/green]  PID={status['pid']}")
    if status.get("started_at"):
        echo(f"[dim]启动时间: {status['started_at']}[/dim]")
    if status.get("worker_pid"):
        echo(f"[dim]worker PID: {status['worker_pid']}[/dim]")
    echo(f"[dim]重启次数: {status.get('restart_count', 0)}[/dim]")
    if status.get("last_exit_code") is not None:
        echo(f"[dim]上次退出码: {status['last_exit_code']}[/dim]")
    echo(f"[dim]日志: {status['log']}[/dim]")


@feishu_group.command("logs")
@click.option("--tail", type=int, default=50, show_default=True, help="展示末尾多少行")
def logs_cmd(tail: int) -> None:
    """查看飞书后台日志。"""
    if not LOG_FILE.exists():
        echo("[dim]暂无日志[/dim]")
        return
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise click.ClickException(f"读取日志失败：{safe(exc)}") from exc
    lines = text.splitlines()
    if tail > 0 and len(lines) > tail:
        lines = lines[-tail:]
    click.echo("\n".join(lines))


@feishu_group.command("handle-event")
def handle_event_cmd() -> None:
    """内部入口：处理一条飞书消息事件并输出 JSON 回复。"""
    side_output = io.StringIO()
    try:
        raw = sys.stdin.read() or "{}"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("payload 必须是 JSON 对象。")
        with contextlib.redirect_stdout(side_output):
            reply = handle_event_payload(payload)
    except Exception as exc:
        reply = _handle_event_error_reply(str(exc))
    extra = side_output.getvalue().strip()
    if extra:
        click.echo(extra, err=True)
    sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
    sys.stdout.flush()
