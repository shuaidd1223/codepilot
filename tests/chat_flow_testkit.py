"""Shared setup helpers for chat/clarification flow tests."""

from __future__ import annotations

from codepilot import db
from codepilot import webui as webui_mod


def init_test_db(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    db.init_db()
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0


def register_project(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "README.md").write_text("# Demo", encoding="utf-8")
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)
    return project_path
