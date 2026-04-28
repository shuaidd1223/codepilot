"""Shared state helpers for Web UI action modules."""

from __future__ import annotations

import sys

from codepilot.core.config import load_project_config
from codepilot.webapp.display_sort import sort_jobs_for_display
from codepilot.webapp.payloads import _now_iso

_MAX_EVENTS = 40
_MAX_JOB_LOG_LINES = 50
_GOAL_MAX_BYTES = 4096


def _shell():
    """Return the active UI-state shell module for state access."""
    shell = sys.modules.get("codepilot.webapp.server")
    if shell is not None:
        return shell
    return sys.modules.get("codepilot.webapp.actions")


def _effective_planner(project_info: dict, planner: str | None = None) -> str:
    explicit = (planner or "").strip()
    if explicit:
        return explicit
    cfg = load_project_config(project_info)
    if cfg and getattr(cfg, "automation", None):
        configured = (cfg.automation.planner or "").strip()
        if configured:
            return configured
    return "codex"


def _append_event(message: str, *, level: str = "info", project: str | None = None, task_id: int | None = None) -> None:
    shell = _shell()
    if shell is None:
        return
    entry = {
        "time": _now_iso(),
        "level": level,
        "project": project or "",
        "task_id": task_id,
        "message": message,
    }
    with shell._UI_LOCK:
        shell._UI_EVENTS.append(entry)
        del shell._UI_EVENTS[:-_MAX_EVENTS]


def _normalize_goal_category(category: str | None) -> str:
    normalized = (category or "auto").lower()
    valid_categories = {"auto", "question", "task", "requirement", "command"}
    if normalized not in valid_categories:
        return "auto"
    return normalized


def _format_numbered_questions(questions: list[dict], render_questions) -> str:
    return render_questions(questions)


def _extract_job_task_ids(result: dict) -> list[int]:
    job = result.get("job")
    if not isinstance(job, dict):
        return []
    task_ids = job.get("task_ids") or []
    return task_ids if isinstance(task_ids, list) else []


def _next_job_id() -> int:
    shell = _shell()
    with shell._UI_LOCK:
        shell._UI_JOB_SEQ += 1
        return shell._UI_JOB_SEQ


def _update_job(job_id: int, **fields) -> dict:
    shell = _shell()
    with shell._UI_LOCK:
        job = shell._UI_JOBS.setdefault(job_id, {"id": job_id})
        job.update(fields)
        return dict(job)


def list_ui_jobs(project: str | None = None) -> list[dict]:
    shell = _shell()
    with shell._UI_LOCK:
        items = [dict(job) for job in shell._UI_JOBS.values()]
    if project:
        items = [job for job in items if job.get("project") == project]
    return sort_jobs_for_display(items)[:12]


def list_ui_events(project: str | None = None) -> list[dict]:
    shell = _shell()
    with shell._UI_LOCK:
        items = list(shell._UI_EVENTS)
    if project:
        items = [event for event in items if not event.get("project") or event.get("project") == project]
    return list(reversed(items[-12:]))
