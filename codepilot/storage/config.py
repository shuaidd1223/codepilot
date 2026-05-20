"""Database runtime configuration and connection primitives."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from codepilot.core.paths import global_storage_root


def default_db_path() -> Path:
    """Return the default task DB location under the user's home directory."""
    return global_storage_root() / "tasks.db"


def get_db_path() -> Path:
    """Return effective DB path from env and ensure its parent directory exists."""
    raw_path = Path(os.environ.get("CODEPILOT_DB_PATH", str(default_db_path())))
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    return raw_path


def get_cache_ttl_seconds() -> float:
    """Return query-cache TTL from env, clamped to non-negative values."""
    return max(0.0, float(os.environ.get("CODEPILOT_DB_CACHE_TTL_SECONDS", "1.5")))


def open_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Create a SQLite connection with shared pragma defaults."""
    conn = sqlite3.connect(db_path or get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn
