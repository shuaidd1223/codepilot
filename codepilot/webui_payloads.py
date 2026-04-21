"""Pure payload-building helpers for the Web UI.

Imported and re-exported by :mod:`codepilot.webui` so historical attribute
access (``webui.dashboard_payload``, ``webui.task_detail_payload``) keeps
working. These functions never mutate the shared UI state; they read from the
DB and compose JSON-shaped dicts.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from codepilot import db
from codepilot.runtime import runtime_summary


STATUS_ORDER = {"in_progress": 0, "backlog": 1, "failed": 2, "cancelled": 3, "done": 4}


def _shell():
    """Return the ``codepilot.webui`` shell module for dynamic lookups."""
    return sys.modules["codepilot.webui"]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tail_text(text: str, *, max_lines: int = 160, max_chars: int = 20000) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
    return text[-max_chars:] if len(text) > max_chars else text


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    target = Path(path)
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


def _parse_depends(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [int(item) for item in items if str(item).strip()]


# Cap per-request log delta at 2 MiB so a one-shot `/log?offset=0` on a huge
# file doesn't block the event loop or fill the client buffer. The frontend
# loops on `next_offset` until `done` to page in the rest.
_LOG_CHUNK_MAX_BYTES = 2 * 1024 * 1024


def task_log_delta(task_id: int, *, offset: int = 0) -> dict:
    """Return a `{offset, next_offset, size, text, done, path}` slice of the
    task's current log file starting at *offset* bytes.

    Used by the Web UI to stream the full log incrementally instead of
    re-tailing on every poll — the frontend keeps a running buffer, asks for
    `?offset=<bytes_consumed>` on each update, and appends the returned
    `text` until `done=True`.
    """
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")

    path_str = task.get("current_log_path") or ""
    out: dict = {
        "task_id": task_id,
        "path": path_str,
        "offset": max(0, int(offset or 0)),
        "next_offset": max(0, int(offset or 0)),
        "size": 0,
        "text": "",
        "done": True,
    }

    if not path_str:
        return out

    target = Path(path_str)
    if not target.exists():
        return out

    try:
        size = target.stat().st_size
    except OSError:
        return out

    out["size"] = size
    start = out["offset"]
    if start >= size:
        out["next_offset"] = size
        out["done"] = True
        return out

    # Read only the delta so large tails stay fast. Binary-safe open + decode
    # with replace to tolerate partial multibyte writes.
    end = min(size, start + _LOG_CHUNK_MAX_BYTES)
    try:
        with target.open("rb") as fh:
            fh.seek(start)
            raw = fh.read(end - start)
    except OSError:
        return out

    text = raw.decode("utf-8", errors="replace")
    out["text"] = text
    out["next_offset"] = end
    out["done"] = end >= size
    return out


def daemon_health_payload(*, stale_after_seconds: int = 120) -> dict:
    """Introspect the local daemon's heartbeat file.

    Used by the Web UI to surface a banner when the daemon appears dead —
    tasks would otherwise silently sit in ``backlog`` forever with no visible
    hint that nothing is draining the queue.

    Returns ``{alive, running, pid, last_heartbeat, stale_seconds, reason}``:
    ``alive`` is True iff the daemon process is running AND its heartbeat
    is fresh. ``running`` means only the PID check, so we can distinguish
    "stopped" from "frozen".
    """
    from codepilot.runtime import is_process_alive
    from datetime import datetime

    from codepilot.paths import global_storage_root

    daemon_dir = global_storage_root() / "daemon"
    lock_file = daemon_dir / "daemon.lock"
    heartbeat_file = daemon_dir / "daemon.heartbeat"

    out = {
        "alive": False,
        "running": False,
        "pid": 0,
        "last_heartbeat": "",
        "stale_seconds": 0,
        "stale_after_seconds": int(stale_after_seconds),
        "reason": "daemon 未运行",
    }

    if not lock_file.exists():
        return out
    try:
        pid = int(lock_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        out["reason"] = "daemon.lock 读取失败"
        return out
    out["pid"] = pid
    if not is_process_alive(pid):
        out["reason"] = f"daemon 进程 {pid} 已不存在（可能已崩溃）"
        return out
    out["running"] = True

    if not heartbeat_file.exists():
        # Old daemon build without heartbeat writing, or daemon just
        # started — treat as alive but flag missing heartbeat.
        out["alive"] = True
        out["reason"] = "心跳文件不存在（可能是旧版本 daemon）"
        return out

    try:
        last = heartbeat_file.read_text(encoding="utf-8").strip()
    except OSError:
        out["reason"] = "daemon.heartbeat 读取失败"
        return out
    out["last_heartbeat"] = last
    try:
        delta = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        out["reason"] = "心跳时间戳解析失败"
        return out
    out["stale_seconds"] = int(max(0, delta))
    if delta > stale_after_seconds:
        out["reason"] = f"daemon 心跳 {int(delta)}s 未更新（>{stale_after_seconds}s 阈值），可能已假死"
    else:
        out["alive"] = True
        out["reason"] = ""
    return out


def _compose_log_text(task: dict) -> str:
    live = _read_text(task.get("current_log_path"))
    if live:
        return _tail_text(live)

    logs = db.list_task_logs(task["id"])
    if logs:
        blocks = []
        for entry in logs:
            header = f"[{entry.get('phase') or '-'}] agent={entry.get('agent') or '-'} exit={entry.get('exit_code') if entry.get('exit_code') is not None else '-'}"
            blocks.append(header)
            if entry.get("output"):
                blocks.append(entry["output"].strip())
        return _tail_text("\n\n".join(blocks))

    return _tail_text(task.get("last_output") or task.get("error_message") or task.get("delivery_record") or "")


def _task_payload(task: dict) -> dict:
    status = task["status"]

    # Best-effort ETA for this task based on historical median duration of
    # tasks run by the same agent in the same project. Only surfaces for
    # pending / in-progress tasks — done tasks show actual runtime instead.
    eta_seconds: int | None = None
    if status in {"backlog", "in_progress"}:
        try:
            eta_seconds = db.compute_agent_eta_seconds(task["project"], task.get("agent") or None)
        except Exception:
            eta_seconds = None

    # Preflight skip re-queues the task to backlog and stores the reason in
    # ``error_message``. That isn't a real failure — it's a "postponed, fix
    # this thing and I'll retry" warning. Surface it as ``skip_reason`` so
    # the UI can render it as a neutral / warning block instead of red.
    raw_error = task.get("error_message") or ""
    skip_reason = ""
    error_message = ""
    if status == "backlog" and raw_error:
        skip_reason = raw_error
    else:
        error_message = raw_error

    return {
        "id": task["id"],
        "project": task["project"],
        "title": task["title"],
        "status": status,
        "priority": task["priority"],
        "agent": task["agent"],
        "source": task.get("source") or "user",
        "phase": task.get("run_phase") or "",
        "runtime": runtime_summary(task) if status == "in_progress" else "",
        "eta_seconds": eta_seconds,
        "latest": task.get("last_output") or skip_reason or error_message or task.get("delivery_record") or "",
        "error_message": error_message,
        "skip_reason": skip_reason,
        "delivery_record": task.get("delivery_record") or "",
        "created_at": task.get("created_at") or "",
        "started_at": task.get("started_at") or "",
        "completed_at": task.get("completed_at") or "",
        "retry_count": int(task.get("retry_count") or 0),
        "max_retries": int(task.get("max_retries") or 0),
        "actions": {
            "retry": status in {"failed", "cancelled", "backlog"},
            "stop": status == "in_progress",
            "promote": status in {"backlog", "failed", "cancelled"},
        },
    }


def _sorted_tasks(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda item: (STATUS_ORDER.get(item["status"], 9), item["priority"], item["id"]))


def project_summary(project: dict, *, job_count: int | None = None) -> dict:
    """Summary tile for the sidebar. ``job_count`` is optional because jobs
    live in in-memory shell state (``_UI_JOBS``) — the dashboard entry point
    injects it so we don't pull the shell import from every call site."""
    stats = db.get_task_stats(project["name"])
    tasks = _sorted_tasks(db.list_tasks(project=project["name"]))
    live = next((task for task in tasks if task["status"] == "in_progress"), None)
    session_count = len(db.list_sessions(project=project["name"]))
    return {
        "name": project["name"],
        "path": project["path"],
        "stats": stats,
        "session_count": session_count,
        "job_count": int(job_count or 0),
        "active_summary": runtime_summary(live) if live else "",
    }


def dashboard_payload(selected_project: str | None = None) -> dict:
    """Snapshot of the full workspace for the sidebar + current-project view.

    Returns tasks/jobs **for every known project** (keyed by name) so the
    frontend can hydrate its per-project cache once and then make project
    switching a pure navigation update — no extra round-trip, no
    "wrong-project tasks briefly show up" race.

    ``tasks`` / ``jobs`` (non-plural-keyed) are retained as a convenience
    alias of the currently-selected project's slice so existing callers
    and tests don't break.
    """
    shell = _shell()
    db.init_db()
    project_rows = db.list_projects()

    tasks_by_project: dict[str, list[dict]] = {}
    jobs_by_project: dict[str, list[dict]] = {}
    for proj in project_rows:
        name = proj["name"]
        raw_tasks = _sorted_tasks(db.list_tasks(project=name))
        tasks_by_project[name] = [_task_payload(task) for task in raw_tasks]
        try:
            jobs_by_project[name] = shell.list_ui_jobs(name)
        except Exception:
            jobs_by_project[name] = []

    projects = [
        project_summary(proj, job_count=len(jobs_by_project.get(proj["name"], [])))
        for proj in project_rows
    ]
    resolved = selected_project or (projects[0]["name"] if projects else None)
    return {
        "projects": projects,
        "selected_project": resolved,
        "tasks_by_project": tasks_by_project,
        "jobs_by_project": jobs_by_project,
        # Back-compat aliases for the currently-selected project.
        "tasks": tasks_by_project.get(resolved, []) if resolved else [],
        "jobs": jobs_by_project.get(resolved, []) if resolved else [],
        "events": shell.list_ui_events(resolved),
    }


def task_detail_payload(task_id: int) -> dict:
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    payload = _task_payload(task)
    payload.update(
        {
            "content": task.get("content") or "",
            "depends_on": _parse_depends(task.get("depends_on")),
            "project_path": task.get("project_path") or "",
            "current_log_path": task.get("current_log_path") or "",
            "log_text": _compose_log_text(task),
            "logs": [
                {
                    "phase": entry.get("phase") or "",
                    "agent": entry.get("agent") or "",
                    "exit_code": entry.get("exit_code"),
                    "output_excerpt": _tail_text(entry.get("output") or "", max_lines=40, max_chars=5000),
                }
                for entry in db.list_task_logs(task_id)
            ],
        }
    )
    return payload
