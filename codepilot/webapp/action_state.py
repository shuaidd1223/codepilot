"""Shared state helpers for Web UI action modules."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from codepilot.core.runtime import is_process_alive, stop_process_tree
from codepilot.core.config import load_project_config
from codepilot.storage import database as db
from codepilot.webapp.display_sort import sort_jobs_for_display
from codepilot.webapp.payloads import _now_iso

_MAX_EVENTS = 40
_MAX_JOB_LOG_LINES = 50
_JOB_SERVICE = "webui_job"
_JOB_HISTORY_LIMIT = 200
_ACTIVE_JOB_STATUSES = {"queued", "running", "planning", "cancelling"}


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
    _emit_ui_state_event("event", project=project, task_id=task_id, payload=entry)


def _emit_ui_state_event(kind: str, *, project: str | None = None, task_id: int | None = None, payload: dict | None = None) -> None:
    try:
        from codepilot.core import progress_bus

        message = str((payload or {}).get("message") or (payload or {}).get("line") or kind)
        stage = "job-log" if kind == "job_log" else "ui-state"
        progress_bus.emit(
            stage=stage,
            message=message,
            task_id=task_id,
            level=str((payload or {}).get("level") or "info"),
            event_type=str(kind or "updated"),
            extra={
                "kind": str(kind or "updated"),
                "project": project or "",
                "payload": dict(payload or {}),
            },
        )
    except Exception:  # noqa: BLE001
        # 发送 UI 状态事件失败不应阻止主流程
        pass


def _job_scope(job_id: int) -> str:
    return str(int(job_id))


def _dt_value(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _job_predates_shell_start(job: dict, shell) -> bool:
    status = str(job.get("status") or "").lower()
    if status not in _ACTIVE_JOB_STATUSES:
        return False
    started_at = _dt_value(getattr(shell, "_UI_STARTED_AT", ""))
    if started_at is None:
        return False
    job_dt = _dt_value(job.get("updated_at") or job.get("created_at"))
    return job_dt is not None and job_dt < started_at


def _mark_stale_job_after_restart(job: dict, shell) -> dict:
    if not _job_predates_shell_start(job, shell):
        return job
    runner_pid = _coerce_pid(job.get("runner_pid"))
    if runner_pid and is_process_alive(runner_pid):
        return job
    now = _now_iso()
    log = list(job.get("log") or [])
    line = "需求规划进程已不存在；请确认项目后点击重试重新规划。"
    if not log or log[-1] != line:
        log.append(line)
    updated = {
        **job,
        "status": "failed",
        "phase": "failed",
        "updated_at": now,
        "finished_at": now,
        "error": line,
        "cancel_requested": False,
        "log": log[-_MAX_JOB_LOG_LINES:],
    }
    _persist_job(updated)
    return updated


def _persist_job(job: dict) -> None:
    try:
        job_id = int(job.get("id") or 0)
    except Exception:  # noqa: BLE001
        # 解析 job ID 失败时使用空 ID
        return
    if job_id <= 0:
        return
    try:
        db.upsert_service_state(
            _JOB_SERVICE,
            _job_scope(job_id),
            pid=0,
            status=str(job.get("status") or ""),
            meta=dict(job),
        )
    except Exception:  # noqa: BLE001
        # 持久化 job 状态失败不应阻止主流程
        pass


def _load_persisted_jobs() -> list[dict]:
    try:
        rows = db.list_service_states(_JOB_SERVICE)
    except Exception:  # noqa: BLE001
        # 读取 job 状态失败时使用空列表
        return []
    jobs: list[dict] = []
    for row in rows:
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        if not meta:
            continue
        try:
            meta["id"] = int(meta.get("id") or row.get("scope") or 0)
        except Exception:  # noqa: BLE001
            # 解析 job ID 失败则跳过
            continue
        if int(meta.get("id") or 0) > 0:
            jobs.append(dict(meta))
    return jobs


def _hydrate_jobs_from_store() -> None:
    shell = _shell()
    if shell is None:
        return
    persisted = _load_persisted_jobs()
    if not persisted:
        return
    with shell._UI_LOCK:
        for job in persisted:
            job = _mark_stale_job_after_restart(dict(job), shell)
            job_id = int(job.get("id") or 0)
            if job_id <= 0:
                continue
            current = shell._UI_JOBS.get(job_id)
            persisted_dt = _dt_value(job.get("updated_at"))
            current_dt = _dt_value((current or {}).get("updated_at"))
            if current is None or current_dt is None or (persisted_dt is not None and persisted_dt >= current_dt):
                shell._UI_JOBS[job_id] = dict(job)
        if shell._UI_JOBS:
            shell._UI_JOB_SEQ = max(int(shell._UI_JOB_SEQ or 0), max(int(job_id) for job_id in shell._UI_JOBS))


def _next_job_id() -> int:
    shell = _shell()
    _hydrate_jobs_from_store()
    with shell._UI_LOCK:
        shell._UI_JOB_SEQ += 1
        return shell._UI_JOB_SEQ


def _update_job(job_id: int, **fields) -> dict:
    shell = _shell()
    _hydrate_jobs_from_store()
    with shell._UI_LOCK:
        job = shell._UI_JOBS.setdefault(job_id, {"id": job_id})
        job.update(fields)
        snapshot = dict(job)
    _persist_job(snapshot)
    _emit_ui_state_event(
        "job",
        project=str(snapshot.get("project") or ""),
        payload={
            "id": int(job_id),
            "status": snapshot.get("status"),
            "phase": snapshot.get("phase"),
            "updated_at": snapshot.get("updated_at"),
        },
    )
    return snapshot


def _get_job(job_id: int) -> dict | None:
    _hydrate_jobs_from_store()
    shell = _shell()
    with shell._UI_LOCK:
        job = shell._UI_JOBS.get(int(job_id))
        return dict(job) if job else None


def _is_job_cancel_requested(job_id: int) -> bool:
    job = _get_job(job_id)
    return bool(job and job.get("cancel_requested"))


def _coerce_pid(value: object) -> int:
    try:
        pid = int(value or 0)
    except Exception:  # noqa: BLE001
        # 转换为 PID 失败则返回 0
        return 0
    return pid if pid > 0 else 0


def _job_process_pids(job: dict) -> list[int]:
    pids: list[int] = []
    runner_pid = _coerce_pid(job.get("runner_pid"))
    if runner_pid:
        pids.append(runner_pid)
    raw_planner_pids = job.get("planner_pids")
    if isinstance(raw_planner_pids, list):
        for item in raw_planner_pids:
            pid = _coerce_pid(item)
            if pid:
                pids.append(pid)
    return list(dict.fromkeys(pids))


def _kill_job_process_tree(job: dict) -> list[int]:
    killed: list[int] = []
    for pid in _job_process_pids(job):
        try:
            stop_process_tree(pid, wait_seconds=3)
        except Exception:  # noqa: BLE001
            # 停止进程树失败不应阻止清理
            pass
        killed.append(pid)
    return killed


def cancel_ui_job(job_id: int) -> dict:
    job = _get_job(job_id)
    if not job:
        raise RuntimeError(f"需求 #{job_id} 不存在。")
    status = str(job.get("status") or "")
    if status not in _ACTIVE_JOB_STATUSES:
        raise RuntimeError(f"需求 #{job_id} 当前状态为 {status or '-'}，不能停止。")
    now = _now_iso()
    killed = _kill_job_process_tree(job)
    log = list(job.get("log") or [])
    if killed:
        line = f"已终止需求规划进程树：PID {', '.join(str(pid) for pid in killed)}。"
    else:
        line = "已停止需求规划；未发现可终止的独立规划进程 PID。"
    if not log or log[-1] != line:
        log.append(line)
    fields = {
        "status": "cancelled",
        "phase": "cancelled",
        "updated_at": now,
        "finished_at": now,
        "cancel_requested": True,
        "error": "用户请求停止需求规划",
        "log": log[-_MAX_JOB_LOG_LINES:],
    }
    updated = _update_job(int(job_id), **fields)
    _emit_ui_state_event(
        "job_log",
        project=str(updated.get("project") or ""),
        payload={"id": int(job_id), "line": line, "updated_at": now},
    )
    _append_event(
        f"需求 #{job_id} 已停止。",
        level="warning",
        project=str(updated.get("project") or ""),
    )
    return updated


def list_ui_jobs(project: str | None = None) -> list[dict]:
    shell = _shell()
    _hydrate_jobs_from_store()
    with shell._UI_LOCK:
        items = [dict(job) for job in shell._UI_JOBS.values()]
    if project:
        items = [job for job in items if job.get("project") == project]
    return sort_jobs_for_display(items)[:_JOB_HISTORY_LIMIT]


def list_ui_events(project: str | None = None) -> list[dict]:
    shell = _shell()
    with shell._UI_LOCK:
        items = list(shell._UI_EVENTS)
    if project:
        items = [event for event in items if not event.get("project") or event.get("project") == project]
    return list(reversed(items[-12:]))
