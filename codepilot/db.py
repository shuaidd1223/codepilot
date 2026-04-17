"""SQLite database access for projects, tasks, and task logs."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".codepilot" / "tasks.db"


def _get_db_path() -> Path:
    """Return the effective database path and ensure its parent directory exists."""
    raw_path = Path(os.environ.get("CODEPILOT_DB_PATH", str(DB_PATH)))
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    return raw_path


@contextmanager
def get_conn():
    """Yield a SQLite connection with common pragmas enabled."""
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    """Create tables and run lightweight migrations."""
    with get_conn() as conn:
        conn.executescript(
            """
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
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
            CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id);
            """
        )

        _ensure_column(conn, "tasks", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "max_retries", "INTEGER NOT NULL DEFAULT 3")
        _ensure_column(conn, "tasks", "run_phase", "TEXT")
        _ensure_column(conn, "tasks", "heartbeat_at", "TEXT")
        _ensure_column(conn, "tasks", "active_pid", "INTEGER")
        _ensure_column(conn, "tasks", "current_log_path", "TEXT")
        _ensure_column(conn, "tasks", "last_output", "TEXT")
        _ensure_column(conn, "tasks", "stop_requested", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "stop_reason", "TEXT")
        _ensure_column(conn, "tasks", "source", "TEXT NOT NULL DEFAULT 'user'")
        _ensure_column(conn, "tasks", "dedup_key", "TEXT")
        _ensure_column(conn, "tasks", "fallback_reason", "TEXT")
        conn.commit()


def register_project(
    name: str,
    path: str,
    base_branch: str = "dev",
    default_mode: str = "dual",
    worktree_base: Optional[str] = None,
    config_file: Optional[str] = None,
) -> dict:
    """Register or update a project."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO projects
                (name, path, base_branch, default_mode, worktree_base, config_file)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, path, base_branch, default_mode, worktree_base, config_file),
        )
        conn.commit()
    return get_project(name)


def get_project(name: str) -> Optional[dict]:
    """Fetch a project by name."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def list_projects() -> list[dict]:
    """Return all registered projects."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def find_project_by_path(path: str | Path) -> Optional[dict]:
    """Find the deepest registered project that contains the given path."""
    target = Path(path).resolve()
    matches: list[tuple[int, dict]] = []
    for project in list_projects():
        project_path = Path(project["path"]).resolve()
        try:
            target.relative_to(project_path)
        except ValueError:
            continue
        matches.append((len(project_path.parts), project))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def delete_project(name: str) -> bool:
    """Delete a project and its related tasks."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        conn.execute(
            "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE project = ?)",
            (name,),
        )
        conn.execute("DELETE FROM tasks WHERE project = ?", (name,))
        conn.commit()
        return cur.rowcount > 0


def compute_dedup_key(project: str, title: str, content: str = "") -> str:
    """Return a 16-char hex dedup key: sha256(project + normalized_title + normalized_content)[:16]."""
    normalized = (project.strip() + "|" + title.strip() + "|" + content.strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _find_active_duplicate(conn: sqlite3.Connection, project: str, dedup_key: str) -> Optional[dict]:
    """Return an existing task with the same dedup_key in backlog/in_progress status, or None."""
    row = conn.execute(
        "SELECT * FROM tasks WHERE project = ? AND dedup_key = ? AND status IN ('backlog', 'in_progress') LIMIT 1",
        (project, dedup_key),
    ).fetchone()
    return dict(row) if row else None


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

    with get_conn() as conn:
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
                json.dumps(depends_on) if depends_on else None,
                project_path,
                max_retries,
                source,
                dedup_key,
                fallback_reason,
            ),
        )
        conn.commit()
        task_id = cur.lastrowid
    return get_task(task_id)


def existing_dedup_keys(project: str) -> set[str]:
    """Return dedup_keys already present in active (backlog/in_progress) tasks."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT dedup_key FROM tasks WHERE project = ? AND dedup_key IS NOT NULL "
            "AND status IN ('backlog','in_progress')",
            (project,),
        ).fetchall()
    return {row["dedup_key"] for row in rows if row["dedup_key"]}


def get_task(task_id: int) -> Optional[dict]:
    """Fetch a task by id."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """List tasks with optional filters."""
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY priority ASC, created_at DESC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


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
    if "depends_on" in updates and isinstance(updates["depends_on"], list):
        updates["depends_on"] = json.dumps(updates["depends_on"]) if updates["depends_on"] else None
    if not updates:
        return get_task(task_id)

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values()) + [task_id]

    with get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        conn.commit()
    return get_task(task_id)


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


def next_backlog_task(project: str) -> list[dict]:
    """Return the next runnable backlog task for a project."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.* FROM tasks t
            WHERE t.project = ?
              AND t.status = 'backlog'
              AND (
                  t.depends_on IS NULL
                  OR t.depends_on = ''
                  OR NOT EXISTS (
                      SELECT 1 FROM tasks t2, json_each(t.depends_on) j
                      WHERE CAST(j.value AS INTEGER) = t2.id
                        AND t2.project = t.project
                        AND t2.status != 'done'
                  )
              )
            ORDER BY
                CASE t.priority
                    WHEN 'P0' THEN 1
                    WHEN 'P1' THEN 2
                    WHEN 'P2' THEN 3
                    WHEN 'P3' THEN 4
                    ELSE 5
                END,
                t.created_at ASC
            LIMIT 1
            """,
            (project,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_task_stats(project: str) -> dict:
    """Return aggregate task counts for a project."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM tasks
            WHERE project = ?
            GROUP BY status
            """,
            (project,),
        ).fetchall()
    stats = {row["status"]: row["count"] for row in rows}
    return {
        "backlog": stats.get("backlog", 0),
        "in_progress": stats.get("in_progress", 0),
        "done": stats.get("done", 0),
        "failed": stats.get("failed", 0),
        "cancelled": stats.get("cancelled", 0),
        "total": sum(stats.values()),
    }


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
    with get_conn() as conn:
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
        conn.commit()
        return dict(row)


def list_task_logs(task_id: int) -> list[dict]:
    """List logs for a task."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY started_at, id",
            (task_id,),
        ).fetchall()
        return [dict(row) for row in rows]


# ── Cleanup helpers ─────────────────────────────────────────────────────────


def find_stale_in_progress(
    project: str,
    stale_minutes: int = 30,
) -> list[dict]:
    """Return in_progress tasks whose heartbeat exceeds *stale_minutes*."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE project = ?
              AND status = 'in_progress'
              AND heartbeat_at IS NOT NULL
              AND (julianday('now') - julianday(heartbeat_at)) * 1440 > ?
            """,
            (project, stale_minutes),
        ).fetchall()
        return [dict(r) for r in rows]


def find_orphan_log_paths(project: str) -> list[dict]:
    """Return tasks whose current_log_path is set but the file no longer exists."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE project = ?
              AND current_log_path IS NOT NULL
              AND current_log_path != ''
            """,
            (project,),
        ).fetchall()
        return [dict(r) for r in rows]


def find_old_done_tasks(
    project: str,
    retention_days: int = 30,
) -> list[dict]:
    """Return done tasks older than *retention_days* that still have a log path."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE project = ?
              AND status = 'done'
              AND current_log_path IS NOT NULL
              AND current_log_path != ''
              AND (julianday('now') - julianday(COALESCE(completed_at, created_at))) > ?
            """,
            (project, retention_days),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Session CRUD ───────────────────────────────────────────────────────────


def create_session(
    project: str,
    title: str = "新会话",
) -> dict:
    """Create a new conversation session for a project."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (project, title) VALUES (?, ?)",
            (project, title),
        )
        conn.commit()
        session_id = cur.lastrowid
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row)


def get_session(session_id: int) -> Optional[dict]:
    """Fetch a session by id."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None


def list_sessions(
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """List sessions with optional filters, most recent first."""
    sql = "SELECT * FROM sessions WHERE 1=1"
    params: list = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC, id DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def update_session(session_id: int, **fields) -> Optional[dict]:
    """Update session fields (title, status)."""
    allowed = {"title", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_session(session_id)
    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [session_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE sessions SET {set_clause} WHERE id = ?", values)
        conn.commit()
    return get_session(session_id)


def delete_session(session_id: int) -> bool:
    """Delete a session and its messages."""
    with get_conn() as conn:
        conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cur.rowcount > 0


def create_session_message(
    session_id: int,
    role: str,
    content: str,
    intent: Optional[str] = None,
    task_ids: Optional[list[int]] = None,
) -> dict:
    """Add a message to a session and touch updated_at."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO session_messages (session_id, role, content, intent, task_ids) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, intent, json.dumps(task_ids) if task_ids else None),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        msg_id = cur.lastrowid
        row = conn.execute("SELECT * FROM session_messages WHERE id = ?", (msg_id,)).fetchone()
        return dict(row)


def list_session_messages(session_id: int) -> list[dict]:
    """List messages in a session, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]
