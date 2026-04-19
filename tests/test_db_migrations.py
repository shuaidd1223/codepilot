"""Versioned migration behaviour for codepilot.db."""

from __future__ import annotations

import sqlite3

import pytest

from codepilot import db


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    return db_path


def test_fresh_db_reaches_target_version(fresh_db):
    db.init_db()
    status = db.schema_status()
    assert status["current_version"] == db.SCHEMA_VERSION
    assert status["target_version"] == db.SCHEMA_VERSION
    assert [row["version"] for row in status["applied"]] == list(
        range(1, db.SCHEMA_VERSION + 1)
    )


def test_init_db_is_idempotent(fresh_db):
    db.init_db()
    first = db.schema_status()
    db.init_db()
    second = db.schema_status()
    assert first == second


def test_legacy_db_without_schema_migrations_is_backfilled(monkeypatch, tmp_path):
    """An older DB that already has every current column (via the previous
    `_ensure_column` approach) must be marked as fully migrated without
    re-running anything."""
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))

    # Hand-craft a "legacy" DB: full tasks schema minus schema_migrations.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE projects (
            name TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            base_branch TEXT, default_mode TEXT,
            worktree_base TEXT, config_file TEXT, created_at TEXT
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT, title TEXT, content TEXT, agent TEXT,
            builder TEXT, reviewer TEXT, priority TEXT, depends_on TEXT,
            status TEXT, project_path TEXT, branch_name TEXT,
            worktree_path TEXT, error_message TEXT, delivery_record TEXT,
            retry_count INTEGER, max_retries INTEGER, run_phase TEXT,
            heartbeat_at TEXT, active_pid INTEGER, current_log_path TEXT,
            last_output TEXT, stop_requested INTEGER, stop_reason TEXT,
            source TEXT, dedup_key TEXT, fallback_reason TEXT,
            created_at TEXT, started_at TEXT, completed_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    db.init_db()
    status = db.schema_status()
    assert status["current_version"] == db.SCHEMA_VERSION
    assert len(status["applied"]) == db.SCHEMA_VERSION


def test_partial_legacy_db_applies_missing_migrations(monkeypatch, tmp_path):
    """A legacy DB that stopped at an older column set should receive only
    the still-missing migrations, not re-run the ones whose columns exist."""
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))

    # Minimal pre-retry tasks table (migration 1 and later not yet applied).
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE projects (
            name TEXT PRIMARY KEY, path TEXT UNIQUE,
            base_branch TEXT, default_mode TEXT,
            worktree_base TEXT, config_file TEXT, created_at TEXT
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT, title TEXT, content TEXT, agent TEXT,
            priority TEXT, status TEXT, project_path TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    db.init_db()
    status = db.schema_status()
    assert status["current_version"] == db.SCHEMA_VERSION

    # Every migration-added column should now exist.
    with db.get_conn() as conn:
        for col in (
            "retry_count",
            "max_retries",
            "run_phase",
            "stop_requested",
            "source",
            "dedup_key",
            "fallback_reason",
        ):
            assert db._has_column(conn, "tasks", col), f"missing column: {col}"


def test_schema_migrations_are_strictly_ordered():
    versions = [v for v, _, _ in db._MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions), "duplicate migration versions"
    assert versions[0] == 1, "migrations must start at version 1"
