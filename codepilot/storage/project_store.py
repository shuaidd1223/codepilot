"""SQL helpers for project persistence operations."""

from __future__ import annotations

import sqlite3
from typing import Optional


def upsert_project_by_path(
    conn: sqlite3.Connection,
    *,
    name: str,
    path: str,
    base_branch: str,
    worktree_base: Optional[str],
    config_file: Optional[str],
) -> str:
    """Insert new project row or update an existing row matched by path."""
    existing = conn.execute(
        "SELECT name FROM projects WHERE path = ?",
        (path,),
    ).fetchone()
    if existing:
        effective_name = str(existing["name"])
        conn.execute(
            """
            UPDATE projects
               SET base_branch = ?,
                   worktree_base = ?,
                   config_file = ?
             WHERE path = ?
            """,
            (base_branch, worktree_base, config_file, path),
        )
        return effective_name

    conn.execute(
        """
        INSERT INTO projects
            (name, path, base_branch, worktree_base, config_file)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name, path, base_branch, worktree_base, config_file),
    )
    return name


def fetch_project_by_name(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM projects WHERE name = ?",
        (name,),
    ).fetchone()
    return dict(row) if row else None


def fetch_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def delete_project_with_children(conn: sqlite3.Connection, name: str) -> bool:
    conn.execute(
        "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE project = ?)",
        (name,),
    )
    conn.execute(
        "DELETE FROM session_messages WHERE session_id IN (SELECT id FROM sessions WHERE project = ?)",
        (name,),
    )
    conn.execute("DELETE FROM sessions WHERE project = ?", (name,))
    conn.execute("DELETE FROM tasks WHERE project = ?", (name,))
    cur = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
    return cur.rowcount > 0
