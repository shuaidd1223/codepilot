from __future__ import annotations

from codepilot.storage import database as db
from codepilot.webapp import server as webui_mod


def init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    db.init_db()
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0

