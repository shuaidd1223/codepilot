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
        "latest": task.get("last_output") or task.get("error_message") or task.get("delivery_record") or "",
        "error_message": task.get("error_message") or "",
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


def project_summary(project: dict) -> dict:
    stats = db.get_task_stats(project["name"])
    tasks = _sorted_tasks(db.list_tasks(project=project["name"]))
    live = next((task for task in tasks if task["status"] == "in_progress"), None)
    return {
        "name": project["name"],
        "path": project["path"],
        "stats": stats,
        "active_summary": runtime_summary(live) if live else "",
    }


def dashboard_payload(selected_project: str | None = None) -> dict:
    shell = _shell()
    db.init_db()
    projects = [project_summary(project) for project in db.list_projects()]
    resolved = selected_project or (projects[0]["name"] if projects else None)
    tasks = _sorted_tasks(db.list_tasks(project=resolved)) if resolved else []
    return {
        "projects": projects,
        "selected_project": resolved,
        "tasks": [_task_payload(task) for task in tasks],
        "jobs": shell.list_ui_jobs(resolved),
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
