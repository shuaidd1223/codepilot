from __future__ import annotations

from pathlib import Path

from codepilot.storage import database as db


def _init_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    monkeypatch.setenv("CODEPILOT_HOME", str(tmp_path / "home"))
    db.init_db()
    return db_path


def test_project_lookup_accepts_directory_name_alias(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    project_path = tmp_path / "flower"
    project_path.mkdir()
    db.register_project("codepilot-dev", str(project_path))

    project = db.get_project("flower")

    assert project is not None
    assert project["name"] == "codepilot-dev"
    assert project["path"] == str(project_path)


def test_project_lookup_syncs_config_project_name_for_registered_project(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    project_path = tmp_path / "workspace"
    project_path.mkdir()
    config_file = project_path / "AGENTS.toml"
    config_file.write_text('[project]\nname = "flower"\n', encoding="utf-8")
    db.register_project("codepilot-dev", str(project_path), config_file=str(config_file))

    project = db.get_project("flower")

    assert project is not None
    assert project["name"] == "flower"
    assert project["path"] == str(project_path)
    assert db.get_project("codepilot-dev") is None
