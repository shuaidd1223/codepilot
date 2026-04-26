"""SQLite database access for projects, tasks, and task logs."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

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


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


_SERVICE_STATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_states (
    service      TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT '',
    pid          INTEGER,
    status       TEXT NOT NULL DEFAULT 'running',
    log_path     TEXT,
    heartbeat_at TEXT,
    meta         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (service, scope)
);
CREATE INDEX IF NOT EXISTS idx_service_states_service ON service_states(service);
"""


def _ensure_service_states_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SERVICE_STATES_SCHEMA)


# ── Schema baseline & versioned migrations ─────────────────────────────────
#
# The baseline `_BASELINE_SCHEMA` reflects the *current* canonical schema so
# fresh DBs create everything at once. Each entry in `_MIGRATIONS` upgrades
# an older DB to the numbered version; they are idempotent (use _ensure_column)
# so re-running against an already-upgraded DB is safe. Every applied migration
# writes a row to `schema_migrations` for audit.

_BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name            TEXT PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    base_branch     TEXT NOT NULL DEFAULT 'dev',
    default_mode    TEXT NOT NULL DEFAULT 'dual',
    worktree_base   TEXT,
    config_file     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL REFERENCES projects(name),
    title           TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    agent           TEXT NOT NULL DEFAULT 'dual',
    builder         TEXT,
    reviewer        TEXT,
    priority        TEXT NOT NULL DEFAULT 'P2',
    depends_on      TEXT,
    status          TEXT NOT NULL DEFAULT 'backlog',
    project_path    TEXT NOT NULL,
    branch_name     TEXT,
    worktree_path   TEXT,
    error_message   TEXT,
    delivery_record TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 3,
    run_phase       TEXT,
    heartbeat_at    TEXT,
    active_pid      INTEGER,
    current_log_path TEXT,
    last_output     TEXT,
    stop_requested  INTEGER NOT NULL DEFAULT 0,
    stop_reason     TEXT,
    source          TEXT NOT NULL DEFAULT 'user',
    dedup_key       TEXT,
    fallback_reason TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS task_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent       TEXT,
    phase       TEXT NOT NULL,
    output      TEXT,
    exit_code   INTEGER,
    started_at  TEXT,
    finished_at TEXT,
    duration    INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL REFERENCES projects(name),
    title       TEXT NOT NULL DEFAULT '新会话',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'user',
    content     TEXT NOT NULL DEFAULT '',
    intent      TEXT,
    task_ids    TEXT,
    metadata    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS service_states (
    service     TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '',
    pid         INTEGER,
    status      TEXT NOT NULL DEFAULT 'running',
    log_path    TEXT,
    heartbeat_at TEXT,
    meta        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (service, scope)
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_service_states_service ON service_states(service);
"""


def _mig_1_retry_fields(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "retry_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "tasks", "max_retries", "INTEGER NOT NULL DEFAULT 3")


def _mig_2_runtime_fields(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "run_phase", "TEXT")
    _ensure_column(conn, "tasks", "heartbeat_at", "TEXT")
    _ensure_column(conn, "tasks", "active_pid", "INTEGER")
    _ensure_column(conn, "tasks", "current_log_path", "TEXT")
    _ensure_column(conn, "tasks", "last_output", "TEXT")


def _mig_3_stop_fields(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "stop_requested", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "tasks", "stop_reason", "TEXT")


def _mig_4_task_source(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "source", "TEXT NOT NULL DEFAULT 'user'")


def _mig_5_dedup_key(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "dedup_key", "TEXT")


def _mig_6_fallback_reason(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "fallback_reason", "TEXT")


def _mig_7_service_states(conn: sqlite3.Connection) -> None:
    _ensure_service_states_schema(conn)


def _mig_8_session_message_metadata(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "session_messages", "metadata", "TEXT")


_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "tasks: retry_count / max_retries", _mig_1_retry_fields),
    (2, "tasks: run_phase / heartbeat / active_pid / log_path / last_output", _mig_2_runtime_fields),
    (3, "tasks: stop_requested / stop_reason", _mig_3_stop_fields),
    (4, "tasks: source", _mig_4_task_source),
    (5, "tasks: dedup_key", _mig_5_dedup_key),
    (6, "tasks: fallback_reason", _mig_6_fallback_reason),
    (7, "service_states: daemon/inspect/webui runtime state", _mig_7_service_states),
    (8, "session_messages: metadata for structured clarification", _mig_8_session_message_metadata),
]

SCHEMA_VERSION = max(v for v, _, _ in _MIGRATIONS)


def _get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM schema_migrations"
    ).fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _record_migration(conn: sqlite3.Connection, version: int, description: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations (version, description) VALUES (?, ?)",
        (version, description),
    )


def init_db() -> None:
    """Create baseline tables and run versioned migrations in order."""
    with get_write_conn() as conn:
        conn.executescript(_BASELINE_SCHEMA)

        # Seed schema_migrations on existing pre-versioned DBs by inferring
        # which historical schema changes are already present.
        current = _get_schema_version(conn)
        if current == 0:
            inferred = 0
            migration_checks = [
                _has_column(conn, "tasks", "retry_count") and _has_column(conn, "tasks", "max_retries"),
                _has_column(conn, "tasks", "last_output"),
                _has_column(conn, "tasks", "stop_requested") and _has_column(conn, "tasks", "stop_reason"),
                _has_column(conn, "tasks", "source"),
                _has_column(conn, "tasks", "dedup_key"),
                _has_column(conn, "tasks", "fallback_reason"),
                _has_table(conn, "service_states"),
                _has_column(conn, "session_messages", "metadata"),
            ]
            for version, present in enumerate(migration_checks, 1):
                if not present:
                    break
                inferred = version
            for version, description, _ in _MIGRATIONS:
                if version > inferred:
                    break
                _record_migration(conn, version, description)
            current = inferred

        for version, description, apply in _MIGRATIONS:
            if version <= current:
                continue
            apply(conn)
            _record_migration(conn, version, description)
    _cache_invalidate()


def schema_status() -> dict:
    """Return current schema version and applied migration history."""
    with get_read_conn() as conn:
        rows = conn.execute(
            "SELECT version, description, applied_at "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
        return {
            "current_version": _get_schema_version(conn),
            "target_version": SCHEMA_VERSION,
            "applied": [dict(row) for row in rows],
        }


def register_project(
    name: str,
    path: str,
    base_branch: str = "dev",
    default_mode: str = "dual",
    worktree_base: Optional[str] = None,
    config_file: Optional[str] = None,
) -> dict:
    """Register or update a project.

    Uses UPDATE-by-path / INSERT-new semantics instead of ``INSERT OR REPLACE``
    so that child rows (tasks, sessions) keep their foreign-key references
    intact. If a row already exists for *path*, its existing ``name`` is
    preserved to avoid orphaning dependent tasks.
    """
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
    _cache_invalidate("project_by_name", "projects", "project_by_path", "tasks", "task_by_id", "task_stats", "sessions")
    return get_project(effective_name)


def get_project(name: str) -> Optional[dict]:
    """Fetch a project by name."""
    cache_key = ("project_by_name", name)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_project_by_name(conn, name)
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


def delete_project(name: str) -> bool:
    """Delete a project and its related tasks."""
    with get_write_conn() as conn:
        removed = _delete_project_with_children(conn, name)
    if removed:
        _cache_invalidate("project_by_name", "projects", "project_by_path", "tasks", "task_by_id", "task_logs", "task_stats", "sessions", "session_by_id", "session_messages")
    return removed


def compute_dedup_key(project: str, title: str, content: str = "") -> str:
    """Return a 16-char hex dedup key: sha256(project + normalized_title + normalized_content)[:16]."""
    normalized = (project.strip() + "|" + title.strip() + "|" + content.strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _normalize_depends_on_value(value: object) -> str | None:
    """Normalize depends_on payload to canonical JSON-array text or None.

    Accepts list/tuple/set, scalar ids, JSON strings and accidental
    double-encoded JSON strings (e.g. "\"[]\"").
    """
    if value is None:
        return None

    parsed: object = value
    for _ in range(3):
        if not isinstance(parsed, str):
            break
        text = parsed.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
            break

    if parsed is None:
        return None
    if not isinstance(parsed, (list, tuple, set)):
        parsed = [parsed]

    deps: list[int] = []
    seen: set[int] = set()
    for item in parsed:
        try:
            dep = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if dep <= 0 or dep in seen:
            continue
        seen.add(dep)
        deps.append(dep)

    return json.dumps(deps) if deps else None


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
    """Create a task.  Auto-computes *dedup_key* when not supplied and returns
    an existing backlog/in_progress task instead of inserting a duplicate."""
    if not dedup_key:
        dedup_key = compute_dedup_key(project, title, content)

    if not project_path:
        proj = get_project(project)
        project_path = proj["path"] if proj else ""

    with get_write_conn() as conn:
        existing = _find_active_duplicate(conn, project, dedup_key)
        if existing:
            import click

            click.echo(f"[i] 已存在任务 #{existing['id']}")
            return existing

        cur = conn.execute(
            """
            INSERT INTO tasks
                (project, title, content, agent, priority, depends_on, project_path, max_retries, source, dedup_key, fallback_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project,
                title,
                content,
                agent,
                priority,
                _normalize_depends_on_value(depends_on),
                project_path,
                max_retries,
                source,
                dedup_key,
                fallback_reason,
            ),
        )
        task_id = cur.lastrowid
    _cache_invalidate("tasks", "task_by_id", "task_stats")
    return get_task(task_id)


def existing_dedup_keys(project: str) -> set[str]:
    """Return dedup_keys already present in active (backlog/in_progress) tasks."""
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
    allowed = {
        "title",
        "content",
        "agent",
        "priority",
        "depends_on",
        "status",
        "branch_name",
        "worktree_path",
        "error_message",
        "delivery_record",
        "started_at",
        "completed_at",
        "builder",
        "reviewer",
        "project_path",
        "retry_count",
        "max_retries",
        "run_phase",
        "heartbeat_at",
        "active_pid",
        "current_log_path",
        "last_output",
        "stop_requested",
        "stop_reason",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "depends_on" in updates:
        updates["depends_on"] = _normalize_depends_on_value(updates["depends_on"])
    if not updates:
        return get_task(task_id)

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values()) + [task_id]

    with get_write_conn() as conn:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
    _cache_invalidate("tasks", "task_by_id", "task_stats")
    return get_task(task_id)


def delete_task(task_id: int) -> bool:
    """Delete one task row and its logs."""
    with get_write_conn() as conn:
        conn.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        removed = cur.rowcount > 0
    if removed:
        _cache_invalidate("tasks", "task_by_id", "task_stats")
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

    updates = {
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
    """Return a rough ETA (in seconds) based on historical ``done`` tasks.

    Picks the most recent ``sample_size`` completed tasks for the project
    (optionally filtered by agent) and returns the median wall-clock
    duration between ``started_at`` and ``completed_at``. Returns ``None``
    when there's not enough history to produce a stable number.
    """
    with get_read_conn() as conn:
        rows = _query_done_task_windows(
            conn,
            project,
            agent=agent,
            sample_size=sample_size,
        )

    from datetime import datetime as _dt

    durations: list[float] = []
    for row in rows:
        try:
            started = _dt.fromisoformat(row["started_at"])
            completed = _dt.fromisoformat(row["completed_at"])
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
    """Return task counts for the registered project containing *path* or the current working directory."""
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
        cur = conn.execute(
            """
            INSERT INTO task_logs
                (task_id, agent, phase, output, exit_code, started_at, finished_at, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, agent, phase, output, exit_code, started_at, finished_at, duration),
        )
        log_id = cur.lastrowid
        row = conn.execute("SELECT * FROM task_logs WHERE id = ?", (log_id,)).fetchone()
        result = dict(row)
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


# ── Cleanup helpers ─────────────────────────────────────────────────────────


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


# ── Session CRUD ───────────────────────────────────────────────────────────


def create_session(
    project: str,
    title: str = "新会话",
) -> dict:
    """Create a new conversation session for a project."""
    with get_write_conn() as conn:
        session_id = _insert_session(conn, project, title)
    _cache_invalidate("sessions", "session_by_id", "session_messages")
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
    """Update session fields (title, status)."""
    allowed = {"title", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
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
        deleted_rows = _delete_session_row(conn, session_id)
        removed = deleted_rows > 0
    if removed:
        _cache_invalidate("sessions", "session_by_id", "session_messages")
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
    _cache_invalidate("sessions", "session_by_id", "session_messages")
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


# ── Service Runtime State ───────────────────────────────────────────────────


def _normalize_service_scope(scope: str | None) -> str:
    return (scope or "").strip()


def _decode_service_meta(raw: object) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _service_row_to_dict(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["meta"] = _decode_service_meta(record.get("meta"))
    return record


def _is_missing_service_states_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table: service_states" in str(exc).lower()


def get_service_state(service: str, scope: str = "") -> Optional[dict]:
    """Return one service runtime state row."""
    normalized_scope = _normalize_service_scope(scope)
    cache_key = ("service_state", service, normalized_scope)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        try:
            row = conn.execute(
                "SELECT * FROM service_states WHERE service = ? AND scope = ?",
                (service, normalized_scope),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_missing_service_states_table(exc):
                return _cache_set(cache_key, None)  # type: ignore[return-value]
            raise
        result = _service_row_to_dict(row) if row else None
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_service_states(service: Optional[str] = None) -> list[dict]:
    """List service runtime state rows."""
    cache_key = ("service_states", service or "")
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]

    sql = "SELECT * FROM service_states"
    params: list[str] = []
    if service:
        sql += " WHERE service = ?"
        params.append(service)
    sql += " ORDER BY service ASC, scope ASC"

    with get_read_conn() as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_missing_service_states_table(exc):
                return _cache_set(cache_key, [])  # type: ignore[return-value]
            raise
        result = [_service_row_to_dict(row) for row in rows]
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
    normalized_scope = _normalize_service_scope(scope)
    now_iso = datetime.now().isoformat(timespec="seconds")
    heartbeat_value = heartbeat_at or now_iso
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    with get_write_conn() as conn:
        _ensure_service_states_schema(conn)
        conn.execute(
            """
            INSERT INTO service_states
                (service, scope, pid, status, log_path, heartbeat_at, meta, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, scope) DO UPDATE SET
                pid = excluded.pid,
                status = excluded.status,
                log_path = excluded.log_path,
                heartbeat_at = excluded.heartbeat_at,
                meta = excluded.meta,
                updated_at = excluded.updated_at
            """,
            (
                service,
                normalized_scope,
                pid,
                status,
                log_path,
                heartbeat_value,
                meta_json,
                now_iso,
            ),
        )
    _cache_invalidate("service_state", "service_states")
    state = get_service_state(service, normalized_scope)
    return state or {}


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
    normalized_scope = _normalize_service_scope(scope)
    with get_write_conn() as conn:
        _ensure_service_states_schema(conn)
        cur = conn.execute(
            "DELETE FROM service_states WHERE service = ? AND scope = ?",
            (service, normalized_scope),
        )
        removed = cur.rowcount > 0
    if removed:
        _cache_invalidate("service_state", "service_states")
    return removed

