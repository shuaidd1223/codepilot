from __future__ import annotations

import json
import tomllib
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def test_project_rename_command_updates_project_and_children(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("old-name", str(project_path))
    task = db.create_task("old-name", "rename me", content="body")
    session = db.create_session("old-name", title="chat")

    result = CliRunner().invoke(main, ["project", "rename", "old-name", "new-name", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["old_name"] == "old-name"
    assert payload["new_name"] == "new-name"
    assert db.get_project("old-name") is None
    assert db.get_project("new-name") is not None
    assert db.get_task(task["id"])["project"] == "new-name"
    assert db.get_session(session["id"])["project"] == "new-name"


def test_project_rename_command_updates_agents_toml_project_alias(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    config_path = project_path / "AGENTS.toml"
    config_path.write_text(
        (
            '[project]\nname = "old-name"\nbase_branch = "main"\n\n'
            '[automation]\ntask_workspace = "direct"\n\n'
            '[feishu_bot]\ndefault_project = "old-name"\n'
        ),
        encoding="utf-8",
    )
    db.register_project("old-name", str(project_path), config_file=str(config_path))

    result = CliRunner().invoke(main, ["project", "rename", "old-name", "new-name", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config_updated"] is True
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["project"]["name"] == "new-name"
    assert parsed["project"]["base_branch"] == "main"
    assert parsed["automation"]["task_workspace"] == "direct"
    assert parsed["feishu_bot"]["default_project"] == "new-name"
    assert db.get_project("old-name") is None
    assert db.get_project("new-name") is not None


def test_project_rename_command_rejects_broken_agents_toml_without_db_migration(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    config_path = project_path / "AGENTS.toml"
    config_path.write_text('[project]\nname = "old-name"\n[broken\n', encoding="utf-8")
    db.register_project("old-name", str(project_path), config_file=str(config_path))
    task = db.create_task("old-name", "keep old project", content="body")

    result = CliRunner().invoke(main, ["project", "rename", "old-name", "new-name", "--json"])

    assert result.exit_code != 0
    assert "AGENTS.toml" in result.output
    assert db.get_project("old-name") is not None
    assert db.get_project("new-name") is None
    assert db.get_task(task["id"])["project"] == "old-name"


def test_project_rename_command_rolls_back_when_agents_toml_is_not_writable(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    config_path = project_path / "AGENTS.toml"
    config_path.write_text('[project]\nname = "old-name"\n', encoding="utf-8")
    db.register_project("old-name", str(project_path), config_file=str(config_path))
    task = db.create_task("old-name", "keep old project", content="body")

    original_write_text = Path.write_text

    def fail_config_write(self, *args, **kwargs):
        if Path(self) == config_path:
            raise OSError("read-only config")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_config_write)

    result = CliRunner().invoke(main, ["project", "rename", "old-name", "new-name", "--json"])

    assert result.exit_code != 0
    assert "read-only config" in result.output
    assert db.get_project("old-name") is not None
    assert db.get_project("new-name") is None
    assert db.get_task(task["id"])["project"] == "old-name"


def test_project_rename_command_rejects_duplicate_target(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    db.register_project("first", str(first))
    db.register_project("second", str(second))

    result = CliRunner().invoke(main, ["project", "rename", "first", "second"])

    assert result.exit_code != 0
    assert "已存在" in result.output
    assert db.get_project("first") is not None
    assert db.get_project("second") is not None
