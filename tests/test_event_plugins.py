from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db


def _init_project(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "project"
    project.mkdir()
    result = CliRunner().invoke(main, ["setup", str(project), "--json"])
    assert result.exit_code == 0, result.output
    return project


def test_setup_creates_default_event_sink_registry(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)

    registry_path = project / ".codepilot" / "events" / "sinks.json"
    assert registry_path.is_file()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    assert registry["schema_version"] == 1
    assert registry["sinks"][0]["name"] == "local-jsonl"
    assert registry["sinks"][0]["type"] == "jsonl"
    assert registry["sinks"][0]["enabled"] is False
    assert registry["sinks"][0]["path"] == ".codepilot/events/events.jsonl"


def test_event_list_outputs_registered_sinks_json(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["event", "list", "-p", "project", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "event list"
    assert payload["data"]["project"] == "project"
    assert payload["data"]["sinks"][0]["name"] == "local-jsonl"


def test_event_register_jsonl_sink_and_test_writes_event(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)

    register = CliRunner().invoke(
        main,
        [
            "event",
            "register",
            "-p",
            "project",
            "--name",
            "audit",
            "--type",
            "jsonl",
            "--path",
            ".codepilot/events/audit.jsonl",
            "--event",
            "doctor.checked",
            "--json",
        ],
    )

    assert register.exit_code == 0, register.output
    registered = json.loads(register.output)
    assert registered["data"]["sink"]["name"] == "audit"
    assert registered["data"]["sink"]["enabled"] is True

    test = CliRunner().invoke(main, ["event", "test", "-p", "project", "--event", "doctor.checked", "--json"])

    assert test.exit_code == 0, test.output
    payload = json.loads(test.output)
    assert payload["data"]["delivered"] == 1

    lines = (project / ".codepilot" / "events" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "doctor.checked"
    assert event["source"] == "codepilot.event.test"
    assert event["project"] == "project"


def test_event_register_rejects_paths_outside_project(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        [
            "event",
            "register",
            "-p",
            "project",
            "--name",
            "bad",
            "--type",
            "jsonl",
            "--path",
            "../outside.jsonl",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "event_error"
    assert not (tmp_path / "outside.jsonl").exists()


def test_event_test_skips_disabled_default_sink(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["event", "test", "-p", "project", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["delivered"] == 0
    assert payload["data"]["results"][0]["status"] == "disabled"
    assert db.get_project("project") is not None


def test_event_schema_lists_known_event_contracts(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["event", "schema", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    schemas = {item["type"]: item for item in payload["data"]["schemas"]}

    assert "test.event" in schemas
    assert "doctor.checked" in schemas
    assert "task.updated" in schemas
    assert "required_fields" in schemas["test.event"]
    assert "payload" in schemas["test.event"]["required_fields"]


def test_event_disable_and_enable_toggle_registered_sink(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    register = CliRunner().invoke(
        main,
        [
            "event",
            "register",
            "-p",
            "project",
            "--name",
            "audit",
            "--type",
            "jsonl",
            "--path",
            ".codepilot/events/audit.jsonl",
            "--json",
        ],
    )
    assert register.exit_code == 0, register.output

    disabled = CliRunner().invoke(main, ["event", "disable", "-p", "project", "audit", "--json"])
    assert disabled.exit_code == 0, disabled.output
    assert json.loads(disabled.output)["data"]["sink"]["enabled"] is False

    skipped = CliRunner().invoke(main, ["event", "test", "-p", "project", "--json"])
    assert skipped.exit_code == 0, skipped.output
    skipped_payload = json.loads(skipped.output)
    audit_result = next(item for item in skipped_payload["data"]["results"] if item["name"] == "audit")
    assert audit_result["status"] == "disabled"
    assert not (project / ".codepilot" / "events" / "audit.jsonl").exists()

    enabled = CliRunner().invoke(main, ["event", "enable", "-p", "project", "audit", "--json"])
    assert enabled.exit_code == 0, enabled.output
    assert json.loads(enabled.output)["data"]["sink"]["enabled"] is True

    delivered = CliRunner().invoke(main, ["event", "test", "-p", "project", "--json"])
    assert delivered.exit_code == 0, delivered.output
    assert json.loads(delivered.output)["data"]["delivered"] == 1
    assert (project / ".codepilot" / "events" / "audit.jsonl").is_file()


def test_event_enable_unknown_sink_returns_json_error(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["event", "enable", "-p", "project", "missing", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "event_error"


def test_task_status_update_emits_task_updated_to_enabled_sink(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    register = CliRunner().invoke(
        main,
        [
            "event",
            "register",
            "-p",
            "project",
            "--name",
            "task-audit",
            "--type",
            "jsonl",
            "--path",
            ".codepilot/events/tasks.jsonl",
            "--event",
            "task.updated",
            "--json",
        ],
    )
    assert register.exit_code == 0, register.output

    task = db.create_task("project", "event task", agent="codex")
    updated = db.update_task(task["id"], status="failed", run_phase="review", error_message="boom")

    assert updated["status"] == "failed"
    lines = (project / ".codepilot" / "events" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "task.updated"
    assert event["source"] == "codepilot.task"
    assert event["project"] == "project"
    assert event["payload"]["task_id"] == task["id"]
    assert event["payload"]["status"] == "failed"
    assert event["payload"]["phase"] == "review"
    assert event["payload"]["summary"] == "boom"
    assert set(event["payload"]["changed_fields"]) >= {"status", "run_phase", "error_message"}
