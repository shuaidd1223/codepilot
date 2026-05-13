"""Write-focused SQL helpers for task persistence workflows."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional


TASK_UPDATE_FIELDS = {
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


def _normalize_depends_on_value(value: object) -> str | None:
    """Normalize depends_on payload to canonical JSON-array text or None."""
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


def insert_task_row(
    conn: sqlite3.Connection,
    *,
    project: str,
    title: str,
    content: str,
    agent: str,
    priority: str,
    depends_on: object,
    project_path: str,
    max_retries: int,
    source: str,
    dedup_key: str,
    fallback_reason: Optional[str],
) -> int:
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
    return int(cur.lastrowid)


def prepare_task_updates(fields: dict[str, object]) -> dict[str, object]:
    updates = {key: value for key, value in fields.items() if key in TASK_UPDATE_FIELDS}
    if "depends_on" in updates:
        updates["depends_on"] = _normalize_depends_on_value(updates["depends_on"])
    return updates


def update_task_fields(
    conn: sqlite3.Connection,
    task_id: int,
    updates: dict[str, object],
) -> None:
    set_clause = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values()) + [task_id]
    conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)


def delete_task_with_logs(conn: sqlite3.Connection, task_id: int) -> bool:
    conn.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
    cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return cur.rowcount > 0


def insert_task_log_row(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    agent: str,
    phase: str,
    output: str = "",
    exit_code: Optional[int] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    duration: Optional[int] = None,
) -> dict:
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
    return dict(row)
