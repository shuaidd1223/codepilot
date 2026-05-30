"""Persistence helpers for service runtime state rows."""

from __future__ import annotations

import json
import sqlite3
from typing import Callable, Optional


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


def fetch_service_state(
    conn: sqlite3.Connection,
    service: str,
    scope: str = "",
) -> Optional[dict]:
    normalized_scope = _normalize_service_scope(scope)
    try:
        row = conn.execute(
            "SELECT * FROM service_states WHERE service = ? AND scope = ?",
            (service, normalized_scope),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_service_states_table(exc):
            return None
        raise
    return _service_row_to_dict(row) if row else None


def query_service_states(
    conn: sqlite3.Connection,
    service: Optional[str] = None,
) -> list[dict]:
    sql = "SELECT * FROM service_states"
    params: list[str] = []
    if service:
        sql += " WHERE service = ?"
        params.append(service)
    sql += " ORDER BY service ASC, scope ASC"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_missing_service_states_table(exc):
            return []
        raise
    return [_service_row_to_dict(row) for row in rows]


def upsert_service_state_row(
    conn: sqlite3.Connection,
    ensure_schema: Callable[[sqlite3.Connection], None],
    *,
    service: str,
    scope: str = "",
    pid: Optional[int] = None,
    status: str = "running",
    log_path: Optional[str] = None,
    heartbeat_at: str,
    meta_json: str,
    updated_at: str,
) -> None:
    ensure_schema(conn)
    normalized_scope = _normalize_service_scope(scope)
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
            heartbeat_at,
            meta_json,
            updated_at,
        ),
    )


def insert_service_state_row_if_absent(
    conn: sqlite3.Connection,
    ensure_schema: Callable[[sqlite3.Connection], None],
    *,
    service: str,
    scope: str = "",
    pid: Optional[int] = None,
    status: str = "running",
    log_path: Optional[str] = None,
    heartbeat_at: str,
    meta_json: str,
    updated_at: str,
) -> bool:
    ensure_schema(conn)
    normalized_scope = _normalize_service_scope(scope)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO service_states
            (service, scope, pid, status, log_path, heartbeat_at, meta, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            service,
            normalized_scope,
            pid,
            status,
            log_path,
            heartbeat_at,
            meta_json,
            updated_at,
        ),
    )
    return cur.rowcount > 0


def delete_service_state_row(
    conn: sqlite3.Connection,
    ensure_schema: Callable[[sqlite3.Connection], None],
    *,
    service: str,
    scope: str = "",
) -> bool:
    ensure_schema(conn)
    normalized_scope = _normalize_service_scope(scope)
    cur = conn.execute(
        "DELETE FROM service_states WHERE service = ? AND scope = ?",
        (service, normalized_scope),
    )
    return cur.rowcount > 0
