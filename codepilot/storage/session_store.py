"""SQL primitives for session/message transactional workflows."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional


def fetch_session_by_id(conn: sqlite3.Connection, session_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def query_sessions(
    conn: sqlite3.Connection,
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    sql = "SELECT * FROM sessions WHERE 1=1"
    params: list[str] = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC, created_at DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def insert_session(conn: sqlite3.Connection, project: str, title: str) -> int:
    cur = conn.execute(
        "INSERT INTO sessions (project, title) VALUES (?, ?)",
        (project, title),
    )
    return int(cur.lastrowid)


def update_session_fields(
    conn: sqlite3.Connection,
    session_id: int,
    updates: dict[str, object],
) -> None:
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [session_id]
    conn.execute(f"UPDATE sessions SET {set_clause} WHERE id = ?", values)


def delete_session_messages(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))


def delete_session_row(conn: sqlite3.Connection, session_id: int) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cur.rowcount


def fetch_session_message_by_id(conn: sqlite3.Connection, message_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM session_messages WHERE id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def query_session_messages(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def insert_session_message(
    conn: sqlite3.Connection,
    session_id: int,
    role: str,
    content: str,
    intent: Optional[str],
    task_ids: Optional[list[int]],
    metadata: Optional[dict] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO session_messages (session_id, role, content, intent, task_ids, metadata) VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            role,
            content,
            intent,
            json.dumps(task_ids) if task_ids else None,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
        ),
    )
    return int(cur.lastrowid)


def update_session_message_fields(
    conn: sqlite3.Connection,
    message_id: int,
    updates: dict[str, object],
) -> None:
    allowed = {"content", "intent", "task_ids", "metadata"}
    clean = {key: value for key, value in updates.items() if key in allowed}
    if not clean:
        return
    set_clause = ", ".join(f"{col} = ?" for col in clean)
    values = list(clean.values()) + [message_id]
    conn.execute(f"UPDATE session_messages SET {set_clause} WHERE id = ?", values)


def touch_session_updated_at(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute(
        "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
        (session_id,),
    )
