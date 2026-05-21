from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot.core.event_plugins import register_jsonl_sink
from codepilot.cli import main
from codepilot.core.workflow_state import (
    advance_agent_phase,
    cleanup_agent_session,
    cleanup_workflow_states,
    complete_agent_session,
    complete_workflow,
    create_agent_session,
    fail_agent_session,
    get_agent_session,
    read_workflow_state,
    start_workflow,
    update_agent_session,
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


def test_workflow_state_write_emits_workflow_changed_event(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    register_jsonl_sink(
        project_path,
        name="workflow-audit",
        path=".codepilot/events/workflow.jsonl",
        events=["workflow.changed"],
    )

    state = start_workflow(project_path, mode="plan", session_id="session-events", current_phase="drafting")

    assert state["current_phase"] == "drafting"
    lines = (project_path / ".codepilot" / "events" / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "workflow.changed"
    assert event["source"] == "codepilot.workflow"
    assert event["project"] == "demo"
    assert event["payload"]["mode"] == "plan"
    assert event["payload"]["phase"] == "drafting"
    assert event["payload"]["state_path"] == ".codepilot/state/plan-state.json"


# ── Agent Kernel session tests ──────────────────────────────────────────────


def test_agent_session_create_and_read(tmp_path):
    session = create_agent_session(tmp_path, goal="添加用户登录功能")

    assert session["session_id"]
    assert session["goal"] == "添加用户登录功能"
    assert session["current_phase"] == "intake"
    assert session["blocked_reason"] is None
    assert session["next_actions"] == []
    assert session["linked_task_ids"] == []
    assert session["completed_at"] is None
    assert session["started_at"]
    assert session["updated_at"]
    assert len(session["phase_history"]) == 1
    assert session["phase_history"][0]["phase"] == "intake"
    assert session["phase_history"][0]["exited_at"] is None

    read_back = get_agent_session(tmp_path)
    assert read_back == session


def test_agent_session_advance_phase_appends_history(tmp_path):
    create_agent_session(tmp_path, goal="修复登录 bug")

    advanced = advance_agent_phase(tmp_path, "clarify")
    assert advanced["current_phase"] == "clarify"
    assert len(advanced["phase_history"]) == 2
    assert advanced["phase_history"][0]["phase"] == "intake"
    assert advanced["phase_history"][0]["exited_at"] is not None
    assert advanced["phase_history"][1]["phase"] == "clarify"
    assert advanced["phase_history"][1]["exited_at"] is None

    session = get_agent_session(tmp_path)
    assert session["current_phase"] == "clarify"
    assert len(session["phase_history"]) == 2


def test_agent_session_advance_with_extra_fields(tmp_path):
    create_agent_session(tmp_path, goal="实现搜索功能")

    advance_agent_phase(tmp_path, "plan", linked_task_ids=["task-1", "task-2"])
    session = get_agent_session(tmp_path)
    assert session["current_phase"] == "plan"
    assert session["linked_task_ids"] == ["task-1", "task-2"]


def test_agent_session_blocked_reason(tmp_path):
    create_agent_session(tmp_path, goal="重构用户模块")

    failed = fail_agent_session(tmp_path, blocked_reason="缺少 API 文档", next_actions=["查阅 Swagger", "联系后端团队"])
    assert failed["blocked_reason"] == "缺少 API 文档"
    assert failed["next_actions"] == ["查阅 Swagger", "联系后端团队"]

    session = get_agent_session(tmp_path)
    assert session["blocked_reason"] == "缺少 API 文档"


def test_agent_session_complete_closes_final_phase(tmp_path):
    create_agent_session(tmp_path, goal="部署 CI 流水线")
    advance_agent_phase(tmp_path, "execute")

    completed = complete_agent_session(tmp_path)
    assert completed["completed_at"] is not None
    assert completed["current_phase"] == "completed"
    assert completed["phase_history"][-1]["exited_at"] is not None
    assert completed["phase_history"][-1]["exited_at"] == completed["completed_at"]


def test_agent_session_corrupt_json_returns_none(tmp_path):
    state_dir = tmp_path / ".codepilot" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "agent-session.json").write_text("{corrupt", encoding="utf-8")

    assert get_agent_session(tmp_path) is None


def test_agent_session_missing_file_returns_none(tmp_path):
    assert get_agent_session(tmp_path) is None


def test_agent_session_cleanup_removes_file(tmp_path):
    create_agent_session(tmp_path, goal="临时任务")
    assert get_agent_session(tmp_path) is not None

    removed = cleanup_agent_session(tmp_path)
    assert removed is True
    assert get_agent_session(tmp_path) is None


def test_workflow_status_cli_json_contains_agent_session(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    create_agent_session(project_path, goal="测试 CLI JSON")
    start_workflow(project_path, mode="clarify", session_id="s-cli", current_phase="drafting")

    result = CliRunner().invoke(main, ["workflow", "status", "-p", "demo", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["ok"] is True

    agent = payload["data"]["agent_session"]
    assert agent is not None
    assert agent["goal"] == "测试 CLI JSON"
    assert agent["current_phase"] == "intake"
    assert agent["blocked_reason"] is None
    assert agent["next_actions"] == []
    assert agent["artifact_paths"] is not None

    mode_state = payload["data"]["state"]
    assert mode_state is not None
    assert mode_state["mode"] == "clarify"
    assert mode_state["current_phase"] == "drafting"


def test_workflow_status_cli_json_no_agent_session(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    result = CliRunner().invoke(main, ["workflow", "status", "-p", "demo", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["data"]["agent_session"] is None


def test_workflow_status_human_output_shows_agent_session(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    create_agent_session(project_path, goal="人肉验证")
    result = CliRunner().invoke(main, ["workflow", "status", "-p", "demo"])
    assert result.exit_code == 0, result.output
    assert "Agent Session" in result.output or "Agent" in result.output
    assert "人肉验证" in result.output
    assert "intake" in result.output
