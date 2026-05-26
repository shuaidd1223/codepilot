from __future__ import annotations

import json
import tomllib
from pathlib import Path

from click.testing import CliRunner

from codepilot.core.paths import global_storage_root, project_storage_root
from codepilot.cli import main
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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


def test_project_rename_command_migrates_project_data_dirs_and_log_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_HOME", str(tmp_path / "home"))
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("old-name", str(project_path))

    old_data = project_storage_root(project_name="old-name")
    new_data = project_storage_root(project_name="new-name")
    old_log = _write(old_data / "runs" / "task-1.console.md", "builder output")
    _write(old_data / "task-files" / "task-1.md", "task file")
    old_conflict_text = "old log"
    _write(old_data / "logs" / "codepilot.log", old_conflict_text)
    _write(new_data / "logs" / "codepilot.log", "new log")

    old_daemon_log = _write(global_storage_root() / "daemon" / "old-name" / "daemon.log", "daemon old")
    _write(global_storage_root() / "inspect" / "old-name" / "inspect.log", "inspect old")
    _write(global_storage_root() / "opencode" / "old-name" / "opencode.json", "{}")

    task = db.create_task("old-name", "rename data", content=f"audit text keeps {old_data}")
    db.update_task(task["id"], status="in_progress", current_log_path=str(old_log))
    db.upsert_service_state(
        "daemon",
        "old-name",
        pid=1234,
        status="running",
        log_path=str(old_daemon_log),
        meta={"project": "old-name"},
    )

    result = CliRunner().invoke(main, ["project", "rename", "old-name", "new-name", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data_migrated"] is True
    assert payload["data_error"] == ""
    assert payload["data_conflicts"]
    assert payload["data_backup_path"]
    assert (new_data / "runs" / "task-1.console.md").read_text(encoding="utf-8") == "builder output"
    assert (new_data / "task-files" / "task-1.md").read_text(encoding="utf-8") == "task file"
    assert (new_data / "logs" / "codepilot.log").read_text(encoding="utf-8") == "new log"
    backup_file = Path(payload["data_conflicts"][0]["backup"])
    assert backup_file.read_text(encoding="utf-8") == old_conflict_text
    assert (global_storage_root() / "daemon" / "new-name" / "daemon.log").read_text(encoding="utf-8") == "daemon old"
    assert (global_storage_root() / "inspect" / "new-name" / "inspect.log").read_text(encoding="utf-8") == "inspect old"
    assert (global_storage_root() / "opencode" / "new-name" / "opencode.json").is_file()

    renamed_task = db.get_task(task["id"])
    assert renamed_task["project"] == "new-name"
    assert renamed_task["current_log_path"] == str(new_data / "runs" / "task-1.console.md")
    assert Path(renamed_task["current_log_path"]).is_file()
    assert f"audit text keeps {old_data}" in renamed_task["content"]
    daemon_state = db.get_service_state("daemon", "new-name")
    assert daemon_state["log_path"] == str(global_storage_root() / "daemon" / "new-name" / "daemon.log")


def test_project_rename_reports_pending_cleanup_when_old_runtime_dir_is_locked(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_HOME", str(tmp_path / "home"))
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("old-name", str(project_path))
    old_data = project_storage_root(project_name="old-name")
    old_log = _write(old_data / "runs" / "task.console.md", "still readable")
    task = db.create_task("old-name", "locked dir", content="body")
    db.update_task(task["id"], status="in_progress", current_log_path=str(old_log))

    original_rmtree = db.shutil.rmtree

    def locked_rmtree(path, *args, **kwargs):
        if Path(path) == old_data:
            raise PermissionError("directory is in use")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(db.shutil, "rmtree", locked_rmtree)

    result = db.rename_project("old-name", "new-name")

    assert result["ok"] is True
    assert str(old_data) in result["pending_cleanup"]
    renamed_task = db.get_task(task["id"])
    assert renamed_task["project"] == "new-name"
    assert renamed_task["current_log_path"] == str(project_storage_root(project_name="new-name") / "runs" / "task.console.md")
    assert Path(renamed_task["current_log_path"]).is_file()
    assert old_data.exists()


def test_project_rename_same_storage_slug_keeps_existing_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_HOME", str(tmp_path / "home"))
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("same name", str(project_path))
    log_path = _write(project_storage_root(project_name="same name") / "runs" / "same.log", "same slug")
    task = db.create_task("same name", "same slug", content="body")
    db.update_task(task["id"], current_log_path=str(log_path))

    result = db.rename_project("same name", "same-name")

    assert result["ok"] is True
    assert result["data_migrated"] is False
    assert log_path.read_text(encoding="utf-8") == "same slug"
    renamed_task = db.get_task(task["id"])
    assert renamed_task["project"] == "same-name"
    assert renamed_task["current_log_path"] == str(log_path)


def test_project_rename_keeps_old_runtime_root_when_another_project_shares_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_HOME", str(tmp_path / "home"))
    init_test_db(tmp_path, monkeypatch)
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    db.register_project("old name", str(first_path))
    db.register_project("old-name", str(second_path))

    shared_root = project_storage_root(project_name="old name")
    shared_log = _write(shared_root / "runs" / "shared.console.md", "shared runtime data")
    task = db.create_task("old name", "shared slug", content="body")
    db.update_task(task["id"], current_log_path=str(shared_log))

    result = db.rename_project("old name", "renamed")

    assert result["ok"] is True
    assert shared_root.exists()
    assert shared_log.read_text(encoding="utf-8") == "shared runtime data"
    assert str(shared_root) in result["pending_cleanup"]
    assert db.get_project("old-name") is not None
    renamed_task = db.get_task(task["id"])
    assert renamed_task["project"] == "renamed"
    assert renamed_task["current_log_path"] == str(project_storage_root(project_name="renamed") / "runs" / "shared.console.md")


def test_project_rename_leaves_orphaned_task_log_path_unrewritten(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_HOME", str(tmp_path / "home"))
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("old-name", str(project_path))
    orphaned_log = project_storage_root(project_name="old-name") / "runs" / "missing.console.md"
    orphaned_log.parent.mkdir(parents=True, exist_ok=True)
    task = db.create_task("old-name", "orphaned task log", content="body")
    db.update_task(task["id"], current_log_path=str(orphaned_log))

    result = db.rename_project("old-name", "new-name")

    assert result["ok"] is True
    assert result["updated_log_paths"] == 0
    assert result["orphaned_log_paths"]
    assert result["orphaned_log_paths"][0]["path"] == str(orphaned_log)
    renamed_task = db.get_task(task["id"])
    assert renamed_task["project"] == "new-name"
    assert renamed_task["current_log_path"] == str(orphaned_log)
    assert renamed_task["current_log_path"] != str(project_storage_root(project_name="new-name") / "runs" / "missing.console.md")


def test_project_rename_leaves_orphaned_service_log_path_unrewritten(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_HOME", str(tmp_path / "home"))
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("old-name", str(project_path))
    orphaned_log = global_storage_root() / "daemon" / "old-name" / "missing.log"
    orphaned_log.parent.mkdir(parents=True, exist_ok=True)
    db.upsert_service_state(
        "daemon",
        "old-name",
        pid=1234,
        status="running",
        log_path=str(orphaned_log),
        meta={"project": "old-name"},
    )

    result = db.rename_project("old-name", "new-name")

    assert result["ok"] is True
    assert result["updated_service_states"] == 1
    assert result["orphaned_log_paths"]
    assert result["orphaned_log_paths"][0]["path"] == str(orphaned_log)
    daemon_state = db.get_service_state("daemon", "new-name")
    assert daemon_state["log_path"] == str(orphaned_log)
    assert daemon_state["meta"]["project"] == "new-name"
    assert db.get_service_state("daemon", "old-name") is None


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
