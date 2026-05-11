"""SQLite database access for projects, tasks, and task logs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from codepilot.storage.config import (
    default_db_path as _cfg_default_db_path,
    get_cache_ttl_seconds as _cfg_get_cache_ttl_seconds,
    get_db_path as _cfg_get_db_path,
    open_connection as _cfg_open_connection,
)
from codepilot.storage.project_store import (
    delete_project_with_children as _delete_project_with_children,
    fetch_project_by_name as _fetch_project_by_name,
    fetch_projects as _fetch_projects,
    upsert_project_by_path as _upsert_project_by_path,
)
from codepilot.storage.schema_store import (
    SCHEMA_VERSION,
    _BASELINE_SCHEMA,
    _MIGRATIONS,
    _SERVICE_STATES_SCHEMA,
    _ensure_column,
    _ensure_service_states_schema,
    _get_schema_version,
    _has_column,
    _has_table,
    _record_migration,
    initialize_schema as _initialize_schema,
    load_schema_status as _load_schema_status,
)
from codepilot.storage.service_state_store import (
    _decode_service_meta,
    _is_missing_service_states_table,
    _normalize_service_scope,
    _service_row_to_dict,
    delete_service_state_row as _delete_service_state_row,
    fetch_service_state as _fetch_service_state,
    insert_service_state_row_if_absent as _insert_service_state_row_if_absent,
    query_service_states as _query_service_states,
    upsert_service_state_row as _upsert_service_state_row,
)
from codepilot.storage.session_store import (
    delete_session_messages as _delete_session_messages,
    delete_session_row as _delete_session_row,
    fetch_session_by_id as _fetch_session_by_id,
    fetch_session_message_by_id as _fetch_session_message_by_id,
    insert_session as _insert_session,
    insert_session_message as _insert_session_message,
    query_session_messages as _query_session_messages,
    query_sessions as _query_sessions,
    touch_session_updated_at as _touch_session_updated_at,
    update_session_fields as _update_session_fields,
)
from codepilot.storage.task_read_model import (
    fetch_active_dedup_keys as _fetch_active_dedup_keys,
    fetch_task_by_id as _fetch_task_by_id,
    find_active_duplicate as _find_active_duplicate,
    query_done_task_windows as _query_done_task_windows,
    query_next_backlog_task as _query_next_backlog_task,
    query_old_done_tasks as _query_old_done_tasks,
    query_orphan_log_paths as _query_orphan_log_paths,
    query_stale_in_progress as _query_stale_in_progress,
    query_task_logs as _query_task_logs,
    query_task_status_counts as _query_task_status_counts,
    query_tasks as _query_tasks,
)
from codepilot.storage.task_write_store import (
    _normalize_depends_on_value,
    delete_task_with_logs as _delete_task_with_logs,
    insert_task_log_row as _insert_task_log_row,
    insert_task_row as _insert_task_row,
    prepare_task_updates as _prepare_task_updates,
    update_task_fields as _update_task_fields,
)


DB_PATH = _cfg_default_db_path()

_CACHE_TTL_SECONDS = _cfg_get_cache_ttl_seconds()
_CACHE_MISS = object()
_QUERY_CACHE: dict[tuple, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple) -> object:
    if _CACHE_TTL_SECONDS <= 0:
        return _CACHE_MISS
    now = time.time()
    with _CACHE_LOCK:
        payload = _QUERY_CACHE.get(key)
        if not payload:
            return _CACHE_MISS
        cached_at, value = payload
        if now - cached_at > _CACHE_TTL_SECONDS:
            _QUERY_CACHE.pop(key, None)
            return _CACHE_MISS
        return copy.deepcopy(value)


def _cache_set(key: tuple, value: object) -> object:
    if _CACHE_TTL_SECONDS <= 0:
        return value
    with _CACHE_LOCK:
        _QUERY_CACHE[key] = (time.time(), copy.deepcopy(value))
    return value


def _cache_invalidate(*prefixes: str) -> None:
    with _CACHE_LOCK:
        if not prefixes:
            _QUERY_CACHE.clear()
            return
        targets = set(prefixes)
        for key in list(_QUERY_CACHE):
            if key and key[0] in targets:
                _QUERY_CACHE.pop(key, None)


def _invalidate_project_caches() -> None:
    _cache_invalidate(
        "project_by_name",
        "projects",
        "project_by_path",
        "tasks",
        "task_by_id",
        "task_stats",
        "sessions",
    )


def _invalidate_task_caches(*extra_prefixes: str) -> None:
    _cache_invalidate("tasks", "task_by_id", "task_stats", *extra_prefixes)


def _invalidate_session_caches() -> None:
    _cache_invalidate("sessions", "session_by_id", "session_messages")


def _invalidate_service_state_caches() -> None:
    _cache_invalidate("service_state", "service_states")


def _default_db_path() -> Path:
    """Compatibility shim for existing tests and private imports."""
    return _cfg_default_db_path()


def _get_db_path() -> Path:
    """Compatibility shim for existing tests and private imports."""
    return _cfg_get_db_path()


@contextmanager
def get_conn():
    """Compatibility shim: use the read-query connection entrypoint."""
    with get_read_conn() as conn:
        yield conn


@contextmanager
def get_read_conn():
    """Yield a SQLite connection for read-query paths."""
    conn = _cfg_open_connection(_get_db_path())
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_write_conn():
    """Yield a SQLite connection wrapped in one write transaction."""
    conn = _cfg_open_connection(_get_db_path())
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create baseline tables and run versioned migrations in order."""
    with get_write_conn() as conn:
        _initialize_schema(conn)
    _cache_invalidate()


def schema_status() -> dict:
    """Return current schema version and applied migration history."""
    with get_read_conn() as conn:
        return _load_schema_status(conn)


def register_project(
    name: str,
    path: str,
    base_branch: str = "dev",
    default_mode: str = "dual",
    worktree_base: Optional[str] = None,
    config_file: Optional[str] = None,
) -> dict:
    """Register or update a project."""
    with get_write_conn() as conn:
        effective_name = _upsert_project_by_path(
            conn,
            name=name,
            path=path,
            base_branch=base_branch,
            default_mode=default_mode,
            worktree_base=worktree_base,
            config_file=config_file,
        )
    _invalidate_project_caches()
    return get_project(effective_name)


def get_project(name: str) -> Optional[dict]:
    """Fetch a project by registered name or a stable project alias.

    The registered name remains the canonical key used by tasks and service
    state. For user-facing lookup, also accept the registered directory name,
    full project path, and the `[project].name` value from that project's
    config file when they point to exactly one registered project.
    """
    ref = str(name or "").strip()
    if not ref:
        return None
    cache_key = ("project_by_name", ref)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_project_by_name(conn, ref)
    if result is None:
        result = _find_project_by_alias(ref)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_projects() -> list[dict]:
    """Return all registered projects."""
    cache_key = ("projects",)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_projects(conn)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def find_project_by_path(path: str | Path) -> Optional[dict]:
    """Find the deepest registered project that contains the given path."""
    target = Path(path).resolve()
    cache_key = ("project_by_path", str(target))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]

    matches: list[tuple[int, dict]] = []
    for project in list_projects():
        project_path = Path(project["path"]).resolve()
        try:
            target.relative_to(project_path)
        except ValueError:
            continue
        matches.append((len(project_path.parts), project))

    if not matches:
        return _cache_set(cache_key, None)  # type: ignore[return-value]
    matches.sort(key=lambda item: item[0], reverse=True)
    return _cache_set(cache_key, matches[0][1])  # type: ignore[return-value]


def _find_project_by_alias(ref: str) -> Optional[dict]:
    if _looks_like_path(ref):
        by_path = find_project_by_path(Path(ref).expanduser())
        if by_path:
            return by_path

    normalized = ref.casefold()
    matches: list[dict] = []
    for project in list_projects():
        aliases = _project_aliases(project)
        if normalized in aliases:
            matches.append(project)

    if len(matches) == 1:
        return matches[0]
    return None


def _looks_like_path(ref: str) -> bool:
    if not ref:
        return False
    if os.sep in ref or (os.altsep and os.altsep in ref):
        return True
    return bool(Path(ref).drive)


def _project_aliases(project: dict) -> set[str]:
    aliases: set[str] = set()
    path_text = str(project.get("path") or "").strip()
    if path_text:
        path = Path(path_text)
        aliases.add(path.name.casefold())
        aliases.add(str(path.resolve()).casefold())

    config_name = _read_project_config_name(project)
    if config_name:
        aliases.add(config_name.casefold())
    return {alias for alias in aliases if alias}


def _read_project_config_name(project: dict) -> str:
    config_text = str(project.get("config_file") or "").strip()
    if not config_text:
        return ""
    config_path = Path(config_text).expanduser()
    if config_path.is_dir():
        config_path = config_path / "AGENTS.toml"
    if not config_path.is_file():
        return ""
    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    project_data = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project_data, dict):
        return ""
    return str(project_data.get("name") or "").strip()


def delete_project(name: str) -> bool:
    """Delete a project and its related tasks."""
    with get_write_conn() as conn:
        removed = _delete_project_with_children(conn, name)
    if removed:
        _invalidate_project_caches()
        _invalidate_task_caches("task_logs")
        _invalidate_session_caches()
    return removed


def compute_dedup_key(project: str, title: str, content: str = "") -> str:
    """Return a 16-char hex dedup key for project/title/content."""
    normalized = (project.strip() + "|" + title.strip() + "|" + content.strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _resolve_project_path(project: str, project_path: Optional[str]) -> str:
    if project_path:
        return project_path
    proj = get_project(project)
    return proj["path"] if proj else ""


_TASK_EVENT_FIELDS = {
    "status",
    "run_phase",
    "error_message",
    "delivery_record",
    "started_at",
    "completed_at",
    "retry_count",
    "heartbeat_at",
    "active_pid",
    "current_log_path",
    "last_output",
    "stop_requested",
    "stop_reason",
}


def _task_update_summary(task: dict) -> str:
    for field in ("error_message", "delivery_record", "last_output"):
        value = str(task.get(field) or "").strip()
        if value:
            return value[:4000]
    status = str(task.get("status") or "").strip()
    phase = str(task.get("run_phase") or "").strip()
    if status and phase:
        return f"{status}:{phase}"
    return status or phase


def _publish_task_updated_event(task: Optional[dict], changed_fields: set[str]) -> None:
    if not task or not (changed_fields & _TASK_EVENT_FIELDS):
        return
    try:
        from codepilot.core.event_plugins import build_event, dispatch_event_to_sinks

        project_name = str(task.get("project") or "")
        if not project_name:
            return
        project_root = str(task.get("project_path") or "")
        if not project_root:
            project = get_project(project_name)
            project_root = str((project or {}).get("path") or "")
        if not project_root:
            return

        payload = {
            "task_id": int(task.get("id")),
            "status": str(task.get("status") or ""),
            "phase": str(task.get("run_phase") or ""),
            "summary": _task_update_summary(task),
            "changed_fields": sorted(changed_fields),
            "retry_count": int(task.get("retry_count") or 0),
            "max_retries": int(task.get("max_retries") or 0),
            "error_message": str(task.get("error_message") or ""),
            "completed_at": task.get("completed_at"),
        }
        event = build_event(
            project_name,
            "task.updated",
            source="codepilot.task",
            payload=payload,
            event_id_prefix=f"task-{task.get('id')}",
        )
        dispatch_event_to_sinks(project_root, event)
    except Exception:
        return


def create_task(
    project: str,
    title: str,
    content: str = "",
    agent: str = "dual",
    priority: str = "P2",
    depends_on: Optional[list[int]] = None,
    project_path: Optional[str] = None,
    max_retries: int = 3,
    source: str = "user",
    dedup_key: Optional[str] = None,
    fallback_reason: Optional[str] = None,
) -> dict:
    """Create a task or return the existing active duplicate."""
    dedup_key = dedup_key or compute_dedup_key(project, title, content)
    resolved_project_path = _resolve_project_path(project, project_path)

    with get_write_conn() as conn:
        existing = _find_active_duplicate(conn, project, dedup_key)
        if existing:
            import click

            click.echo(f"[i] 已存在任务 #{existing['id']}")
            return existing

        task_id = _insert_task_row(
            conn,
            project=project,
            title=title,
            content=content,
            agent=agent,
            priority=priority,
            depends_on=depends_on,
            project_path=resolved_project_path,
            max_retries=max_retries,
            source=source,
            dedup_key=dedup_key,
            fallback_reason=fallback_reason,
        )
    _invalidate_task_caches()
    return get_task(task_id)


def existing_dedup_keys(project: str) -> set[str]:
    """Return dedup_keys already present in active tasks."""
    with get_read_conn() as conn:
        return _fetch_active_dedup_keys(conn, project)


def get_task(task_id: int) -> Optional[dict]:
    """Fetch a task by id."""
    cache_key = ("task_by_id", int(task_id))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_task_by_id(conn, task_id)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_tasks(
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """List tasks with optional filters."""
    cache_key = ("tasks", project or "", status or "")
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_tasks(conn, project, status)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def update_task(task_id: int, **fields) -> Optional[dict]:
    """Update a task with the provided field values."""
    updates = _prepare_task_updates(fields)
    if not updates:
        return get_task(task_id)

    with get_write_conn() as conn:
        _update_task_fields(conn, task_id, updates)
    _invalidate_task_caches()
    updated = get_task(task_id)
    _publish_task_updated_event(updated, set(updates))
    return updated


def delete_task(task_id: int) -> bool:
    """Delete one task row and its logs."""
    with get_write_conn() as conn:
        removed = _delete_task_with_logs(conn, task_id)
    if removed:
        _invalidate_task_caches()
    return removed


def increment_task_retry(task_id: int, error_message: str) -> dict:
    """Increment retry counter and move the task to backlog or failed."""
    task = get_task(task_id)
    if not task:
        raise ValueError(f"Task {task_id} does not exist")

    retry_count = int(task.get("retry_count") or 0) + 1
    max_retries = int(task.get("max_retries") or 3)
    next_status = "failed" if retry_count >= max_retries else "backlog"
    return update_task(
        task_id,
        retry_count=retry_count,
        status=next_status,
        error_message=error_message,
        run_phase=None,
        heartbeat_at=None,
        active_pid=None,
        current_log_path=None,
        last_output=None,
        stop_requested=0,
        stop_reason=None,
    )


def reset_task_for_retry(task_id: int, *, reset_retry_count: bool = True) -> dict:
    """Reset a task into backlog so it can be manually retried."""
    task = get_task(task_id)
    if not task:
        raise ValueError(f"Task {task_id} does not exist")

    updates: dict[str, object] = {
        "status": "backlog",
        "branch_name": None,
        "worktree_path": None,
        "error_message": None,
        "delivery_record": None,
        "started_at": None,
        "completed_at": None,
        "run_phase": None,
        "heartbeat_at": None,
        "active_pid": None,
        "current_log_path": None,
        "last_output": None,
        "stop_requested": 0,
        "stop_reason": None,
    }
    if reset_retry_count:
        updates["retry_count"] = 0
    return update_task(task_id, **updates)


def next_backlog_task(project: str, *, exclude_task_ids: Optional[set[int]] = None) -> list[dict]:
    """Return the next runnable backlog task for a project."""
    with get_read_conn() as conn:
        return _query_next_backlog_task(conn, project, exclude_task_ids=exclude_task_ids)


def compute_agent_eta_seconds(
    project: str,
    agent: Optional[str] = None,
    *,
    sample_size: int = 20,
) -> Optional[int]:
    """Return a rough ETA in seconds from recent completed tasks."""
    with get_read_conn() as conn:
        rows = _query_done_task_windows(
            conn,
            project,
            agent=agent,
            sample_size=sample_size,
        )

    durations: list[float] = []
    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
            completed = datetime.fromisoformat(row["completed_at"])
        except (ValueError, TypeError):
            continue
        delta = (completed - started).total_seconds()
        if delta > 0:
            durations.append(delta)

    if len(durations) < 3:
        return None

    durations.sort()
    mid = len(durations) // 2
    if len(durations) % 2:
        return int(durations[mid])
    return int((durations[mid - 1] + durations[mid]) / 2)


def get_task_stats(project: str) -> dict:
    """Return aggregate task counts for a project."""
    cache_key = ("task_stats", project)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        stats = _query_task_status_counts(conn, project)
    result = {
        "backlog": stats.get("backlog", 0),
        "in_progress": stats.get("in_progress", 0),
        "done": stats.get("done", 0),
        "failed": stats.get("failed", 0),
        "cancelled": stats.get("cancelled", 0),
        "total": sum(stats.values()),
    }
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def get_task_stats_by_path(path: str | Path) -> Optional[dict]:
    """Return task counts for the deepest registered project containing *path*."""
    project = find_project_by_path(path)
    if not project:
        return None
    return get_task_stats(project["name"])


def get_current_project_task_stats(path: str | Path | None = None) -> Optional[dict]:
    """Return task counts for the registered project containing *path* or cwd."""
    return get_task_stats_by_path(path or Path.cwd())


def get_current_project_stats(path: str | Path | None = None) -> Optional[dict]:
    """Backward-compatible alias for current-project task counts."""
    return get_current_project_task_stats(path)


def create_task_log(
    task_id: int,
    agent: str,
    phase: str,
    output: str = "",
    exit_code: Optional[int] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    duration: Optional[int] = None,
) -> dict:
    """Create a task execution log row."""
    with get_write_conn() as conn:
        result = _insert_task_log_row(
            conn,
            task_id=task_id,
            agent=agent,
            phase=phase,
            output=output,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            duration=duration,
        )
    _cache_invalidate("task_logs")
    return result


def list_task_logs(task_id: int) -> list[dict]:
    """List logs for a task."""
    cache_key = ("task_logs", int(task_id))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_task_logs(conn, task_id)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def find_stale_in_progress(
    project: str,
    stale_minutes: int = 30,
) -> list[dict]:
    """Return in_progress tasks whose heartbeat exceeds *stale_minutes*."""
    with get_read_conn() as conn:
        return _query_stale_in_progress(conn, project, stale_minutes)


def find_orphan_log_paths(project: str) -> list[dict]:
    """Return tasks whose current_log_path is set but the file no longer exists."""
    with get_read_conn() as conn:
        return _query_orphan_log_paths(conn, project)


def find_old_done_tasks(
    project: str,
    retention_days: int = 30,
) -> list[dict]:
    """Return done tasks older than *retention_days* that still have a log path."""
    with get_read_conn() as conn:
        return _query_old_done_tasks(conn, project, retention_days)


def create_session(
    project: str,
    title: str = "新会话",
) -> dict:
    """Create a new conversation session for a project."""
    with get_write_conn() as conn:
        session_id = _insert_session(conn, project, title)
    _invalidate_session_caches()
    return get_session(session_id)  # type: ignore[return-value]


def get_session(session_id: int) -> Optional[dict]:
    """Fetch a session by id."""
    cache_key = ("session_by_id", int(session_id))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_session_by_id(conn, session_id)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_sessions(
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """List sessions with optional filters, most recent first."""
    cache_key = ("sessions", project or "", status or "")
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_sessions(conn, project, status)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def update_session(session_id: int, **fields) -> Optional[dict]:
    """Update session fields."""
    updates = {key: value for key, value in fields.items() if key in {"title", "status"}}
    if not updates:
        return get_session(session_id)
    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with get_write_conn() as conn:
        _update_session_fields(conn, session_id, updates)
    _cache_invalidate("sessions", "session_by_id")
    return get_session(session_id)


def delete_session(session_id: int) -> bool:
    """Delete a session and its messages."""
    with get_write_conn() as conn:
        _delete_session_messages(conn, session_id)
        removed = _delete_session_row(conn, session_id) > 0
    if removed:
        _invalidate_session_caches()
    return removed


def create_session_message(
    session_id: int,
    role: str,
    content: str,
    intent: Optional[str] = None,
    task_ids: Optional[list[int]] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Add a message to a session and touch updated_at."""
    with get_write_conn() as conn:
        msg_id = _insert_session_message(conn, session_id, role, content, intent, task_ids, metadata)
        _touch_session_updated_at(conn, session_id)
        result = _fetch_session_message_by_id(conn, msg_id)
    _invalidate_session_caches()
    return result  # type: ignore[return-value]


def list_session_messages(session_id: int) -> list[dict]:
    """List messages in a session, oldest first."""
    cache_key = ("session_messages", int(session_id))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_session_messages(conn, session_id)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def get_service_state(service: str, scope: str = "") -> Optional[dict]:
    """Return one service runtime state row."""
    normalized_scope = _normalize_service_scope(scope)
    cache_key = ("service_state", service, normalized_scope)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_service_state(conn, service, normalized_scope)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_service_states(service: Optional[str] = None) -> list[dict]:
    """List service runtime state rows."""
    cache_key = ("service_states", service or "")
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_service_states(conn, service)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def upsert_service_state(
    service: str,
    scope: str = "",
    *,
    pid: Optional[int] = None,
    status: str = "running",
    log_path: Optional[str] = None,
    heartbeat_at: Optional[str] = None,
    meta: Optional[dict] = None,
) -> dict:
    """Create or update a service runtime state row."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    normalized_scope = _normalize_service_scope(scope)
    with get_write_conn() as conn:
        _upsert_service_state_row(
            conn,
            _ensure_service_states_schema,
            service=service,
            scope=normalized_scope,
            pid=pid,
            status=status,
            log_path=log_path,
            heartbeat_at=heartbeat_at or now_iso,
            meta_json=json.dumps(meta or {}, ensure_ascii=False),
            updated_at=now_iso,
        )
    _invalidate_service_state_caches()
    state = get_service_state(service, normalized_scope)
    return state or {}


def claim_service_state(
    service: str,
    scope: str = "",
    *,
    pid: Optional[int] = None,
    status: str = "running",
    log_path: Optional[str] = None,
    heartbeat_at: Optional[str] = None,
    meta: Optional[dict] = None,
) -> bool:
    """Atomically create one service-state row; return False when it already exists."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    normalized_scope = _normalize_service_scope(scope)
    with get_write_conn() as conn:
        claimed = _insert_service_state_row_if_absent(
            conn,
            _ensure_service_states_schema,
            service=service,
            scope=normalized_scope,
            pid=pid,
            status=status,
            log_path=log_path,
            heartbeat_at=heartbeat_at or now_iso,
            meta_json=json.dumps(meta or {}, ensure_ascii=False),
            updated_at=now_iso,
        )
    if claimed:
        _invalidate_service_state_caches()
    return claimed


def touch_service_state(
    service: str,
    scope: str = "",
    *,
    pid: Optional[int] = None,
    log_path: Optional[str] = None,
    status: str = "running",
) -> dict:
    """Refresh only heartbeat/PID/log_path while preserving existing meta."""
    normalized_scope = _normalize_service_scope(scope)
    existing = get_service_state(service, normalized_scope) or {}
    meta = existing.get("meta") if isinstance(existing.get("meta"), dict) else {}
    next_pid = pid if pid is not None else existing.get("pid")
    next_log = log_path if log_path is not None else existing.get("log_path")
    return upsert_service_state(
        service,
        normalized_scope,
        pid=next_pid,
        status=status,
        log_path=next_log,
        meta=meta,
    )


def clear_service_state(service: str, scope: str = "") -> bool:
    """Delete one service runtime state row."""
    with get_write_conn() as conn:
        removed = _delete_service_state_row(
            conn,
            _ensure_service_states_schema,
            service=service,
            scope=scope,
        )
    if removed:
        _invalidate_service_state_caches()
    return removed
