"""Display-oriented ordering helpers shared by Web UI and CLI output."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


TASK_STATUS_ORDER = {
    "in_progress": 0,
    "backlog": 1,
    "failed": 2,
    "cancelled": 3,
    "done": 4,
}

_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_DONE_JOB_STATUSES = {"succeeded", "attention", "failed", "done", "cancelled"}
_ACTIVE_JOB_STATUS_ORDER = {"running": 0, "planning": 1, "queued": 2}
_ZERO_DT = (0, 0, 0, 0, 0, 0, 0)


def _priority_rank(value: object) -> int:
    return _PRIORITY_ORDER.get(str(value or "P2").upper(), 9)


def _int_key(value: object, default: int = 10**9) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _dt_key(value: object) -> tuple[int, int, int, int, int, int, int]:
    text = str(value or "").strip()
    if not text:
        return _ZERO_DT
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _ZERO_DT
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond)


def _desc_dt_key(value: object) -> tuple[int, int, int, int, int, int, int]:
    key = _dt_key(value)
    return tuple(-part for part in key)


def sort_tasks_for_display(tasks: Iterable[dict]) -> list[dict]:
    """Sort tasks for list views.

    - 非 completed: status 分组内按 priority -> created_at
    - completed(done): 先按 completed_at(倒序) -> priority -> id/created_at
    """

    def _task_key(task: dict) -> tuple:
        status = str(task.get("status") or "")
        status_rank = TASK_STATUS_ORDER.get(status, 9)
        priority_rank = _priority_rank(task.get("priority"))
        task_id = _int_key(task.get("id"))
        created_key = _dt_key(task.get("created_at"))

        if status == "done":
            done_key = _desc_dt_key(task.get("completed_at") or task.get("created_at"))
            return (status_rank, 0, done_key, priority_rank, task_id, created_key)

        return (status_rank, 1, _ZERO_DT, priority_rank, created_key, task_id)

    return sorted(tasks, key=_task_key)


def sort_jobs_for_display(jobs: Iterable[dict]) -> list[dict]:
    """Sort requirement jobs with task-like semantics."""

    def _job_key(job: dict) -> tuple:
        status = str(job.get("status") or "").lower()
        priority_rank = _priority_rank(job.get("priority"))
        job_id = _int_key(job.get("id"))
        created_key = _dt_key(job.get("created_at"))
        finished = status in _DONE_JOB_STATUSES or bool(str(job.get("finished_at") or "").strip())

        if finished:
            done_key = _desc_dt_key(
                job.get("finished_at") or job.get("updated_at") or job.get("created_at")
            )
            return (1, done_key, priority_rank, job_id, created_key)

        active_rank = _ACTIVE_JOB_STATUS_ORDER.get(status, 9)
        return (0, priority_rank, created_key, active_rank, job_id)

    return sorted(jobs, key=_job_key)


def sort_sessions_for_display(sessions: Iterable[dict]) -> list[dict]:
    """Sort sessions by activity time then creation time (both desc)."""

    return sorted(
        sessions,
        key=lambda item: (
            _desc_dt_key(item.get("updated_at")),
            _desc_dt_key(item.get("created_at")),
            -_int_key(item.get("id"), default=0),
        ),
    )

