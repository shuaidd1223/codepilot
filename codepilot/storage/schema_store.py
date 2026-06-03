"""Schema baseline and migration helpers for SQLite storage."""

from __future__ import annotations

import sqlite3
from typing import Callable


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


_BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name            TEXT PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    base_branch     TEXT NOT NULL DEFAULT 'dev',
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

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id);
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_dedup ON tasks(project, dedup_key)")


def _mig_6_fallback_reason(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "fallback_reason", "TEXT")


def _mig_7_service_states(conn: sqlite3.Connection) -> None:
    _ensure_service_states_schema(conn)


def _mig_8_session_message_metadata(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "session_messages", "metadata", "TEXT")


def _mig_9_drop_default_mode(conn: sqlite3.Connection) -> None:
    if _has_column(conn, "projects", "default_mode"):
        conn.execute("ALTER TABLE projects DROP COLUMN default_mode")


_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "tasks: retry_count / max_retries", _mig_1_retry_fields),
    (2, "tasks: run_phase / heartbeat / active_pid / log_path / last_output", _mig_2_runtime_fields),
    (3, "tasks: stop_requested / stop_reason", _mig_3_stop_fields),
    (4, "tasks: source", _mig_4_task_source),
    (5, "tasks: dedup_key", _mig_5_dedup_key),
    (6, "tasks: fallback_reason", _mig_6_fallback_reason),
    (7, "service_states: daemon/inspect/webui runtime state", _mig_7_service_states),
    (8, "session_messages: metadata for structured clarification", _mig_8_session_message_metadata),
    (9, "projects: drop default_mode (replaced by automation.task_agent)", _mig_9_drop_default_mode),
]

SCHEMA_VERSION = max(version for version, _, _ in _MIGRATIONS)


def _get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _record_migration(conn: sqlite3.Connection, version: int, description: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations (version, description) VALUES (?, ?)",
        (version, description),
    )


def _infer_legacy_schema_version(conn: sqlite3.Connection) -> int:
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
    return inferred


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create baseline tables and run versioned migrations in order."""
    conn.executescript(_BASELINE_SCHEMA)

    current = _get_schema_version(conn)
    if current == 0:
        inferred = _infer_legacy_schema_version(conn)
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


def load_schema_status(conn: sqlite3.Connection) -> dict:
    """Return current schema version and applied migration history."""
    rows = conn.execute(
        "SELECT version, description, applied_at "
        "FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {
        "current_version": _get_schema_version(conn),
        "target_version": SCHEMA_VERSION,
        "applied": [dict(row) for row in rows],
    }
