"""Runtime helpers for task execution visibility, stop control, and stale detection."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import time
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from codepilot import db

HEARTBEAT_INTERVAL_SECONDS = 3
STALE_AFTER_SECONDS = 600


def tail_text(path: str | Path | None, *, max_lines: int = 12, max_chars: int = 1200) -> str:
    """Return a compact tail snippet for live status updates."""
    if not path:
        return ""
    candidate = Path(path)
    if not candidate.exists():
        return ""
    text = candidate.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return ""
    lines = text.splitlines()
    snippet = "\n".join(lines[-max_lines:])
    return snippet[-max_chars:]


def is_process_alive(pid: Optional[int]) -> bool:
    """Return whether a process id still exists."""
    if not pid:
        return False

    system = platform.system().lower()
    if system == "windows":
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-Command",
                    f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).Id",
                ],
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
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _windows_process_snapshot() -> dict[int, dict[str, str | int]]:
    """Return a lightweight process table snapshot on Windows."""
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name | "
        "ConvertTo-Json -Compress"
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
        return {}

    raw = (result.stdout or "").strip()
    if result.returncode != 0 or not raw:
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    rows = payload if isinstance(payload, list) else [payload]
    snapshot: dict[int, dict[str, str | int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            proc_id = int(row.get("ProcessId"))
            parent_id = int(row.get("ParentProcessId") or 0)
        except Exception:
            continue
        snapshot[proc_id] = {
            "parent": parent_id,
            "name": str(row.get("Name") or ""),
        }
    return snapshot


def _windows_collect_descendants(root_pid: int, snapshot: dict[int, dict[str, str | int]]) -> list[int]:
    """Collect descendant process IDs for a root process ID."""
    children: dict[int, list[int]] = {}
    for proc_id, info in snapshot.items():
        parent = int(info.get("parent") or 0)
        children.setdefault(parent, []).append(proc_id)

    result: list[int] = []
    stack = list(children.get(int(root_pid), []))
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        stack.extend(children.get(current, []))
    return result


def _windows_kill_pid(pid: int, *, include_tree: bool = False) -> None:
    """Force-kill a single PID (optionally with descendants) on Windows."""
    cmd = ["taskkill", "/PID", str(int(pid))]
    if include_tree:
        cmd.append("/T")
    cmd.append("/F")
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        pass


def stop_process_tree(pid: Optional[int], *, wait_seconds: int = 5) -> bool:
    """Stop a process tree and return whether it is no longer alive."""
    if not pid:
        return True

    pid = int(pid)
    system = platform.system().lower()
    if system == "windows":
        _windows_kill_pid(pid, include_tree=True)

        deadline = time.monotonic() + max(wait_seconds, 1)
        while time.monotonic() < deadline:
            snapshot = _windows_process_snapshot()
            if (not is_process_alive(pid)) and (not _windows_collect_descendants(pid, snapshot)):
                return True
            time.sleep(0.2)

        # Fallback: parent may have crashed already; explicitly walk and kill descendants.
        snapshot = _windows_process_snapshot()
        descendants = _windows_collect_descendants(pid, snapshot)
        for child_pid in sorted(set(descendants), reverse=True):
            _windows_kill_pid(child_pid, include_tree=True)
        _windows_kill_pid(pid, include_tree=False)

        deadline = time.monotonic() + max(wait_seconds, 1)
        while time.monotonic() < deadline:
            snapshot = _windows_process_snapshot()
            descendants = _windows_collect_descendants(pid, snapshot)
            if (not is_process_alive(pid)) and (not descendants):
                return True
            for child_pid in descendants:
                _windows_kill_pid(child_pid, include_tree=True)
            if is_process_alive(pid):
                _windows_kill_pid(pid, include_tree=False)
            time.sleep(0.2)

        snapshot = _windows_process_snapshot()
        return (not is_process_alive(pid)) and (not _windows_collect_descendants(pid, snapshot))

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True

    deadline = time.monotonic() + max(wait_seconds, 1)
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.2)

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    return not is_process_alive(pid)


def update_task_runtime(
    task_id: int,
    *,
    phase: Optional[str] = None,
    pid: Optional[int] = None,
    log_path: str | Path | None = None,
    last_output: Optional[str] = None,
    heartbeat_at: Optional[datetime] = None,
) -> dict | None:
    """Persist live runtime metadata for a task."""
    updates = {}
    if phase is not None:
        updates["run_phase"] = phase
    if pid is not None:
        updates["active_pid"] = pid
    if log_path is not None:
        updates["current_log_path"] = str(log_path)
    if last_output is not None:
        updates["last_output"] = last_output
    updates["heartbeat_at"] = (heartbeat_at or datetime.now()).isoformat()
    return db.update_task(task_id, **updates)


def clear_task_runtime(task_id: int, **extra_fields) -> dict | None:
    """Clear live runtime metadata when a task finishes or is cancelled."""
    return db.update_task(
        task_id,
        run_phase=None,
        heartbeat_at=None,
        active_pid=None,
        current_log_path=None,
        last_output=None,
        **extra_fields,
    )


def request_task_stop(task_id: int, reason: str = "") -> dict | None:
    """Mark a task for cancellation; the runner will pick this up on the next heartbeat."""
    return db.update_task(task_id, stop_requested=1, stop_reason=(reason or "已收到停止请求"))


def clear_task_stop(task_id: int) -> dict | None:
    """Clear any pending stop request."""
    return db.update_task(task_id, stop_requested=0, stop_reason=None)


def get_stop_request(task_id: int) -> tuple[bool, str]:
    """Return whether a task has a pending stop request and its reason."""
    task = db.get_task(task_id)
    if not task:
        return False, ""
    return bool(task.get("stop_requested")), (task.get("stop_reason") or "").strip()


def describe_runtime_age(timestamp: Optional[str]) -> str:
    """Render a compact relative time for status output."""
    if not timestamp:
        return "-"
    try:
        delta = max(0, int((datetime.now() - datetime.fromisoformat(timestamp)).total_seconds()))
    except ValueError:
        return "-"

    if delta < 60:
        return f"{delta}s前"
    if delta < 3600:
        return f"{delta // 60}m前"
    return f"{delta // 3600}h前"


def runtime_summary(task: dict, *, stale_after_seconds: int = STALE_AFTER_SECONDS) -> str:
    """Return a concise status summary for in-progress tasks."""
    if task.get("status") != "in_progress":
        return "-"

    heartbeat_at = task.get("heartbeat_at") or task.get("started_at")
    heartbeat_text = describe_runtime_age(heartbeat_at)
    phase = task.get("run_phase") or "running"
    pid = task.get("active_pid")
    requested = bool(task.get("stop_requested"))

    stale = False
    if heartbeat_at:
        try:
            stale = (datetime.now() - datetime.fromisoformat(heartbeat_at)).total_seconds() > stale_after_seconds
        except ValueError:
            stale = False

    parts = [phase]
    if pid:
        parts.append(f"pid={pid}")
    parts.append(f"活跃 {heartbeat_text}")
    if stale and (not pid or not is_process_alive(pid)):
        parts.append("疑似卡住")
    if requested:
        parts.append("停止中")
    last_output = (task.get("last_output") or "").replace("\n", " ").strip()
    if last_output:
        parts.append((last_output[:40] + "...") if len(last_output) > 40 else last_output)
    return " / ".join(parts)


def list_live_tasks(project: Optional[str] = None) -> list[dict]:
    """Return running tasks whose worker pid is still alive."""
    tasks = db.list_tasks(project=project, status="in_progress")
    return [task for task in tasks if is_process_alive(task.get("active_pid"))]


def reap_stalled_tasks(project: Optional[str] = None, *, stale_after_seconds: int = STALE_AFTER_SECONDS) -> list[dict]:
    """Fail in-progress tasks whose heartbeat expired and whose process is gone."""
    now = datetime.now()
    reaped: list[dict] = []
    for task in db.list_tasks(project=project, status="in_progress"):
        heartbeat_at = task.get("heartbeat_at") or task.get("started_at")
        if not heartbeat_at:
            continue
        try:
            delta = (now - datetime.fromisoformat(heartbeat_at)).total_seconds()
        except ValueError:
            continue
        if delta <= stale_after_seconds:
            continue
        active_pid = task.get("active_pid")
        if is_process_alive(active_pid):
            continue
        if active_pid:
            stop_process_tree(active_pid)

        message = (
            f"任务运行心跳已超过 {stale_after_seconds} 秒，且执行进程不存在。"
            "系统已将其标记为 failed，避免任务长期卡在 in_progress。"
        )
        reaped.append(
            db.update_task(
                task["id"],
                status="failed",
                error_message=message,
                completed_at=now.isoformat(),
                stop_requested=0,
                stop_reason=None,
                run_phase=None,
                heartbeat_at=None,
                active_pid=None,
                current_log_path=None,
                last_output=None,
            )
        )
    return [task for task in reaped if task]
