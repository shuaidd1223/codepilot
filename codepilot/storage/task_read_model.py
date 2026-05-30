"""Read-focused SQL helpers for task and task-log views."""

from __future__ import annotations

import sqlite3
from typing import Optional


def find_active_duplicate(
    conn: sqlite3.Connection,
    project: str,
    dedup_key: str,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM tasks WHERE project = ? AND dedup_key = ? "
        "AND status IN ('backlog', 'in_progress') LIMIT 1",
        (project, dedup_key),
    ).fetchone()
    return dict(row) if row else None


def fetch_active_dedup_keys(conn: sqlite3.Connection, project: str) -> set[str]:
    rows = conn.execute(
        "SELECT dedup_key FROM tasks WHERE project = ? AND dedup_key IS NOT NULL "
        "AND status IN ('backlog','in_progress')",
        (project,),
    ).fetchall()
    return {str(row["dedup_key"]) for row in rows if row["dedup_key"]}


def fetch_task_by_id(conn: sqlite3.Connection, task_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def query_tasks(
    conn: sqlite3.Connection,
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list[object] = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY priority ASC, created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def query_next_backlog_task(
    conn: sqlite3.Connection,
    project: str,
    exclude_task_ids: Optional[set[int]] = None,
) -> list[dict]:
    excludes = {int(task_id) for task_id in (exclude_task_ids or set()) if int(task_id) > 0}
    exclude_sql = ""
    params: list[object] = [project]
    if excludes:
        placeholders = ", ".join("?" for _ in excludes)
        exclude_sql = f" AND t.id NOT IN ({placeholders})"
        params.extend(sorted(excludes))

    rows = conn.execute(
        f"""
        SELECT t.* FROM tasks t
        WHERE t.project = ?
          AND t.status = 'backlog'
          {exclude_sql}
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
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def query_done_task_windows(
    conn: sqlite3.Connection,
    project: str,
    *,
    agent: Optional[str] = None,
    sample_size: int = 20,
) -> list[dict]:
    if agent:
        rows = conn.execute(
            """
            SELECT started_at, completed_at
            FROM tasks
            WHERE project = ? AND agent = ? AND status = 'done'
              AND started_at IS NOT NULL AND completed_at IS NOT NULL
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (project, agent, sample_size),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT started_at, completed_at
            FROM tasks
            WHERE project = ? AND status = 'done'
              AND started_at IS NOT NULL AND completed_at IS NOT NULL
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (project, sample_size),
        ).fetchall()
    return [dict(row) for row in rows]


def query_task_status_counts(conn: sqlite3.Connection, project: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM tasks
        WHERE project = ?
        GROUP BY status
        """,
        (project,),
    ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def query_task_logs(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM task_logs WHERE task_id = ? ORDER BY started_at, id",
        (task_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def query_stale_in_progress(
    conn: sqlite3.Connection,
    project: str,
    stale_minutes: int,
) -> list[dict]:
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
    return [dict(row) for row in rows]


def query_orphan_log_paths(conn: sqlite3.Connection, project: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM tasks
        WHERE project = ?
          AND current_log_path IS NOT NULL
          AND current_log_path != ''
        """,
        (project,),
    ).fetchall()
    return [dict(row) for row in rows]


def query_old_done_tasks(
    conn: sqlite3.Connection,
    project: str,
    retention_days: int,
) -> list[dict]:
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
    return [dict(row) for row in rows]
