from __future__ import annotations

import sqlite3

from codepilot.storage import config as db_config
from codepilot.storage import database as db


def test_db_config_default_path_lives_at_codepilot_root(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEPILOT_DB_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    assert db_config.get_db_path() == tmp_path / "home" / ".codepilot" / "tasks.db"


def test_db_module_db_path_shim_delegates_to_db_config(monkeypatch, tmp_path):
    db_path = tmp_path / "nested" / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))

    assert db._get_db_path() == db_path
    assert db_path.parent.exists()


def test_open_connection_applies_common_pragmas(tmp_path):
    conn = db_config.open_connection(tmp_path / "tasks.db")
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        conn.close()

