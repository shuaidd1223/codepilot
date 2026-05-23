"""Runtime helpers for task execution visibility, stop control, and stale detection."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
import json
import ctypes
from datetime import datetime
from pathlib import Path
from typing import Optional

from codepilot.storage import database as db
from codepilot.core.text_decode import decode_subprocess_text

HEARTBEAT_INTERVAL_SECONDS = 3
STALE_AFTER_SECONDS = 600

# Windows creation flags
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def codepilot_command(*args: str, module: str = "codepilot") -> list[str]:
    """Build a command that re-enters CodePilot in source or frozen mode."""
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.extend(["-m", module])
    command.extend(str(arg) for arg in args)
    return command


def no_window_kwargs(*, new_process_group: bool = False) -> dict:
    """Subprocess kwargs that prevent console window pop-ups on Windows.

    When the parent process has no console (e.g. the detached `codepilot ui start`
    service), spawning a console-mode child like `claude.exe` or `git.exe` will
    pop a fresh console window unless we pass ``CREATE_NO_WINDOW``.

    On POSIX this returns an empty dict (no console concept), or ``start_new_session=True``
    if ``new_process_group`` is set so the parent can clean up the whole tree.
    """
    if platform.system().lower() == "windows":
        flags = _CREATE_NO_WINDOW
        if new_process_group:
            flags |= _CREATE_NEW_PROCESS_GROUP
        kwargs = {"creationflags": flags}
        # Hide window even if a process accidentally tries to show one.
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = si
        except Exception:
            pass
        return kwargs
    if new_process_group:
        return {"start_new_session": True}
    return {}


def _has_console_window() -> bool:
    """On Windows, return whether this process owns a console. Always True elsewhere."""
    if platform.system().lower() != "windows":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True  # If detection fails, assume we have a console (safer default).


def silence_subprocess_windows_if_detached() -> None:
    """If running without a console (e.g. the detached webui service),
    monkey-patch ``subprocess.Popen`` so every child gets ``CREATE_NO_WINDOW``
    and never pops a black console window.

    Idempotent. No-op on POSIX or in interactive terminals.
    """
    if platform.system().lower() != "windows":
        return
    if _has_console_window():
        return  # interactive — let children inherit the parent console.

    orig_popen = subprocess.Popen
    if getattr(orig_popen, "_codepilot_no_window_patched", False):
        return

    class _SilencedPopen(orig_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            cf = kwargs.get("creationflags", 0) or 0
            if not (cf & _CREATE_NO_WINDOW):
                kwargs["creationflags"] = cf | _CREATE_NO_WINDOW
            if "startupinfo" not in kwargs:
                try:
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = 0  # SW_HIDE
                    kwargs["startupinfo"] = si
                except Exception:
                    pass
            super().__init__(*args, **kwargs)

    _SilencedPopen._codepilot_no_window_patched = True
    subprocess.Popen = _SilencedPopen  # type: ignore[misc]


def _detect_text_encoding(path: Path) -> str:
    """Detect the text encoding of *path* from its leading BOM.

    Returns ``"utf-16"`` when a UTF-16 LE or BE BOM is found; otherwise
    ``"utf-8"``.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return "utf-8"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    return "utf-8"


def tail_text(path: str | Path | None, *, max_lines: int = 12, max_chars: int = 1200) -> str:
    """Return a compact tail snippet for live status updates.

    Automatically detects UTF-16 (LE/BE) vs UTF-8 via BOM so that
    log files written by Windows tools are displayed correctly.
    """
    if not path:
        return ""
    candidate = Path(path)
    if not candidate.exists():
        return ""
    encoding = _detect_text_encoding(candidate)
    text = candidate.read_text(encoding=encoding, errors="replace").strip()
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
        return _windows_is_process_alive(int(pid))

    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _windows_is_process_alive(pid: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _windows_query_processes(*properties: str, timeout: int = 15) -> list[dict[str, object]]:
    """共享的 Windows 进程查询：运行 PowerShell Get-CimInstance 并返回行列表。

    参数:
        properties: 要选择的属性名（如 ``"ProcessId"``、``"Name"``）。
        timeout:    PowerShell 超时秒数。

    返回:
        解析后的行 dict 列表；出错时返回空列表。
    """
    props = ",".join(properties) if properties else "*"
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"Get-CimInstance Win32_Process | "
        f"Select-Object {props} | "
        f"ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", script],
            capture_output=True,
            text=False,
            timeout=timeout,
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
    return [row for row in rows if isinstance(row, dict)]


def _windows_process_snapshot() -> dict[int, dict[str, str | int]]:
    """返回 Windows 进程表的精简快照。"""
    rows = _windows_query_processes("ProcessId", "ParentProcessId", "Name")
    snapshot: dict[int, dict[str, str | int]] = {}
    for row in rows:
        try:
            proc_id = int(row.get("ProcessId") or 0)
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            timeout=15,
        )
    except Exception:
        result = None

    if not is_process_alive(pid):
        return

    # ``taskkill`` can fail with "Access is denied" for some detached Python
    # children even when PowerShell's Stop-Process can terminate them.
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-Command",
                f"Stop-Process -Id {int(pid)} -Force -ErrorAction Stop",
            ],
            capture_output=True,
            text=False,
            timeout=15,
        )
    except Exception:
        if result is None:
            return


def _normalise_path(p: str | Path | None) -> str:
    if not p:
        return ""
    try:
        return str(Path(p).resolve()).replace("\\", "/").rstrip("/").lower()
    except Exception:
        return str(p).replace("\\", "/").rstrip("/").lower()


def _path_contains(haystack: str, needle: str) -> bool:
    """Case-insensitive, slash-normalised substring check."""
    if not haystack or not needle:
        return False
    h = haystack.replace("\\", "/").lower()
    return needle in h


def find_worktree_processes(worktree_path: str | Path) -> list[int]:
    """Return PIDs whose command line or cwd points inside *worktree_path*.

    Used to hunt down long-lived dev servers (``next dev``, ``vite``, etc.)
    that a task's builder agent spun up but never stopped. Runs a single
    PowerShell query on Windows; on POSIX falls back to scanning
    ``/proc/*/cwd`` symlinks and ``/proc/*/cmdline`` contents.

    Returns an empty list on any error — best-effort, never raises.
    """
    needle = _normalise_path(worktree_path)
    if not needle:
        return []

    system = platform.system().lower()
    if system == "windows":
        rows = _windows_query_processes(
            "ProcessId", "ParentProcessId", "Name", "CommandLine", "ExecutablePath",
            timeout=20,
        )
        hits: list[int] = []
        for row in rows:
            blob = " ".join(
                str(row.get(k) or "") for k in ("CommandLine", "ExecutablePath")
            )
            if _path_contains(blob, needle):
                try:
                    hits.append(int(row.get("ProcessId") or 0))
                except Exception:
                    continue
        return hits

    # POSIX — best-effort via /proc.
    hits = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cwd = os.readlink(entry / "cwd")
            except OSError:
                cwd = ""
            cmdline = ""
            try:
                cmdline = (entry / "cmdline").read_text(errors="replace").replace("\x00", " ")
            except OSError:
                pass
            blob = cwd + " " + cmdline
            if _path_contains(blob, needle):
                try:
                    hits.append(int(entry.name))
                except Exception:
                    continue
    except Exception:
        return []
    return hits


def stop_worktree_leftovers(
    worktree_path: str | Path | None,
    *,
    keep_pids: Optional[list[int]] = None,
    wait_seconds: int = 5,
) -> list[int]:
    """Kill processes still running inside *worktree_path* (and their trees).

    Meant to be invoked right after a task finishes so dev servers like
    ``next dev`` / ``vite`` / ``npm run dev`` that the builder agent left
    behind don't linger and pop up console windows forever.

    *keep_pids* lets callers keep specific PIDs alive (e.g. the task runner
    itself if it happens to match). Returns the list of PIDs that were
    targeted. Never raises; failures are silently tolerated since this is
    a cleanup best-effort.
    """
    if not worktree_path:
        return []
    pids = find_worktree_processes(worktree_path)
    if not pids:
        return []
    keep = set(int(p) for p in (keep_pids or []) if p)
    pids = [p for p in pids if p not in keep]
    for pid in pids:
        try:
            stop_process_tree(pid, wait_seconds=wait_seconds)
        except Exception:
            continue
    return pids


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
    """Persist live runtime metadata while a task is actively running."""
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
    return db.update_task_if_status(task_id, "in_progress", **updates)


def clear_task_runtime(task_id: int, **extra_fields) -> dict | None:
    """Clear live runtime metadata when a task finishes or is cancelled.

    Intentionally keeps ``current_log_path`` and ``last_output`` populated so
    the Web UI can still render the final log / preview after the task stops
    running. The pointer is only reset when a brand-new run starts
    (``save_task_runtime``) or when cleanup finds the file missing.
    """
    return db.update_task(
        task_id,
        run_phase=None,
        heartbeat_at=None,
        active_pid=None,
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
    """Recover stale in-progress tasks whose heartbeat expired and process is gone.

    A stale task is treated as a retryable execution failure: increment retry
    count and requeue to ``backlog`` while retries remain; only mark ``failed``
    once ``max_retries`` is exhausted.
    """
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

        retry_count = int(task.get("retry_count") or 0) + 1
        max_retries = max(1, int(task.get("max_retries") or 3))
        exhausted = retry_count >= max_retries
        action_text = (
            "系统已将其标记为 failed，避免任务长期卡在 in_progress。"
            if exhausted
            else f"系统已回退到 backlog，等待自动重试（{retry_count}/{max_retries}）。"
        )
        message = (
            f"任务运行心跳已超过 {stale_after_seconds} 秒，且执行进程不存在。"
            f"{action_text}"
        )
        updated = db.increment_task_retry(task["id"], message[:4000])
        if exhausted:
            updated = db.update_task(
                task["id"],
                completed_at=now.isoformat(),
                stop_requested=0,
                stop_reason=None,
            )
        if updated:
            reaped.append(updated)
    return [task for task in reaped if task]
