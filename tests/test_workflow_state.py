from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.core.workflow_state import (
    cleanup_workflow_states,
    complete_workflow,
    read_workflow_state,
    start_workflow,
    update_workflow_state,
    workflow_dirs,
)
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db as _init_test_db


def test_workflow_state_create_read_update_and_directory_convention(tmp_path):
    state = start_workflow(
        tmp_path,
        mode="clarify",
        session_id="session-1",
        current_phase="collecting",
    )

    dirs = workflow_dirs(tmp_path)
    assert dirs["root"] == tmp_path / ".codepilot"
    assert dirs["state"].is_dir()
    assert dirs["context"].is_dir()
    assert dirs["specs"].is_dir()
    assert dirs["plans"].is_dir()

    assert state["mode"] == "clarify"
    assert state["active"] is True
    assert state["current_phase"] == "collecting"
    assert state["session_id"] == "session-1"
    assert state["context_path"] == str(tmp_path / ".codepilot" / "context" / "session-1.json")
    assert state["artifact_paths"]["spec"] == str(tmp_path / ".codepilot" / "specs" / "session-1.md")
    assert state["artifact_paths"]["plan"] == str(tmp_path / ".codepilot" / "plans" / "session-1.md")
    assert state["completed_at"] is None

    by_mode = read_workflow_state(tmp_path, mode="clarify")
    active = read_workflow_state(tmp_path)
    assert by_mode == state
    assert active == state

    updated = update_workflow_state(tmp_path, "clarify", current_phase="questions_ready")

    assert updated["started_at"] == state["started_at"]
    assert updated["updated_at"] >= state["updated_at"]
    assert updated["current_phase"] == "questions_ready"
    assert read_workflow_state(tmp_path)["current_phase"] == "questions_ready"


def test_workflow_state_complete_and_cleanup_removes_inactive_completed_state(tmp_path):
    start_workflow(tmp_path, mode="plan", session_id="session-2")

    completed = complete_workflow(tmp_path, "plan")

    assert completed["active"] is False
    assert completed["current_phase"] == "completed"
    assert completed["completed_at"]
    assert read_workflow_state(tmp_path) is None
    assert read_workflow_state(tmp_path, mode="plan") == completed

    removed = cleanup_workflow_states(tmp_path, completed=True)

    assert removed == [tmp_path / ".codepilot" / "state" / "plan-state.json"]
    assert read_workflow_state(tmp_path, mode="plan") is None


def test_workflow_state_corrupt_json_is_tolerated(tmp_path):
    state_dir = tmp_path / ".codepilot" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "active-workflow.json").write_text("{not-json", encoding="utf-8")
    (state_dir / "clarify-state.json").write_text("{not-json", encoding="utf-8")

    assert read_workflow_state(tmp_path) is None
    assert read_workflow_state(tmp_path, mode="clarify") is None


def test_workflow_status_cli_returns_active_state_json(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    start_workflow(project_path, mode="clarify", session_id="session-cli", current_phase="drafting")

    result = CliRunner().invoke(main, ["workflow", "status", "-p", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "workflow status"
    assert payload["data"]["project"] == "demo"
    assert payload["data"]["state"]["mode"] == "clarify"
    assert payload["data"]["state"]["current_phase"] == "drafting"
