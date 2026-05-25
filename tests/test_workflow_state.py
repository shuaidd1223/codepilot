from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot.commands import add as add_cmd
from codepilot.core.event_plugins import register_jsonl_sink
from codepilot.cli import main
from codepilot.core.memory import read_memory_candidates, read_memory_events
from codepilot.core.workflow_state import (
    advance_agent_phase,
    cleanup_agent_session,
    cleanup_workflow_states,
    complete_agent_session,
    complete_workflow,
    create_agent_session,
    fail_agent_session,
    get_agent_session,
    read_task_execution_artifacts,
    read_workflow_state,
    start_workflow,
    task_execution_artifact_path,
    update_agent_session,
    update_workflow_state,
    write_task_execution_artifacts,
    workflow_dirs,
)
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db as _init_test_db


def _register_demo_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "README.md").write_text("doctor and explore commands live here\n", encoding="utf-8")
    return db.register_project("demo", str(project_path))


def test_workflow_state_create_read_update_and_directory_convention(tmp_path):
    state = start_workflow(
        tmp_path,
        mode="plan",
        session_id="session-1",
        current_phase="collecting",
    )

    dirs = workflow_dirs(tmp_path)
    assert dirs["root"] == tmp_path / ".codepilot"
    assert dirs["state"].is_dir()
    assert dirs["context"].is_dir()
    assert dirs["specs"].is_dir()
    assert dirs["plans"].is_dir()

    assert state["mode"] == "plan"
    assert state["active"] is True
    assert state["current_phase"] == "collecting"
    assert state["session_id"] == "session-1"
    assert state["context_path"] == str(tmp_path / ".codepilot" / "context" / "session-1.json")
    assert state["artifact_paths"]["spec"] == str(tmp_path / ".codepilot" / "specs" / "session-1.md")
    assert state["artifact_paths"]["plan"] == str(tmp_path / ".codepilot" / "plans" / "session-1.md")
    assert state["completed_at"] is None

    by_mode = read_workflow_state(tmp_path, mode="plan")
    active = read_workflow_state(tmp_path)
    assert by_mode == state
    assert active == state

    updated = update_workflow_state(tmp_path, "plan", current_phase="questions_ready")

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
    (state_dir / "plan-state.json").write_text("{not-json", encoding="utf-8")

    assert read_workflow_state(tmp_path) is None
    assert read_workflow_state(tmp_path, mode="plan") is None


def test_task_execution_artifacts_round_trip_project_local_summary(tmp_path):
    written = write_task_execution_artifacts(
        tmp_path,
        7,
        status="done",
        source="run",
        executor="builtin",
        artifacts={
            "patch": {
                "kind": "patch",
                "status": "empty",
                "empty": True,
                "summary": "No git diff detected.",
                "files": [],
            },
            "validation": {
                "kind": "validation",
                "status": "passed",
                "checks": [{"command": "builder", "exit_code": 0, "ok": True}],
            },
            "review": {
                "kind": "review",
                "status": "pass",
                "verdict": "pass",
                "summary": "VERDICT: PASS",
            },
        },
    )

    artifact_path = task_execution_artifact_path(tmp_path, 7)
    assert artifact_path == tmp_path / ".codepilot" / "artifacts" / "tasks" / "task-7-execution.json"
    assert written["artifact_path"] == str(artifact_path)

    payload = read_task_execution_artifacts(tmp_path, 7)
    assert payload is not None
    assert payload["task_id"] == 7
    assert payload["status"] == "done"
    assert payload["source"] == "run"
    assert payload["executor"] == "builtin"
    assert payload["artifacts"]["patch"]["empty"] is True
    assert payload["artifacts"]["validation"]["checks"][0]["exit_code"] == 0
    assert payload["artifacts"]["review"]["verdict"] == "pass"


def test_workflow_status_cli_returns_active_state_json(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    start_workflow(project_path, mode="plan", session_id="session-cli", current_phase="drafting")

    result = CliRunner().invoke(main, ["workflow", "status", "-p", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "workflow status"
    assert payload["data"]["project"] == "demo"
    assert payload["data"]["state"]["mode"] == "plan"
    assert payload["data"]["state"]["current_phase"] == "drafting"
    assert payload["data"]["auto_policy"] == {
        "allow_create_inspect_tasks": False,
        "allow_import_plan_tasks": False,
        "max_steps": 1,
        "failure_threshold": 1,
    }


def test_workflow_auto_policy_cli_shows_defaults_json(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    result = CliRunner().invoke(main, ["workflow", "auto-policy", "-p", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "workflow auto-policy"
    assert payload["data"]["allow_create_inspect_tasks"] is False
    assert payload["data"]["allow_import_plan_tasks"] is False
    assert payload["data"]["max_steps"] == 1
    assert payload["data"]["failure_threshold"] == 1


def test_workflow_auto_policy_cli_reflects_agents_toml_overrides(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[automation]
workflow_auto_create_inspect_tasks = true
workflow_auto_import_plan_tasks = true
workflow_auto_max_steps = 5
workflow_auto_failure_threshold = 3
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["workflow", "auto-policy", "-p", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["allow_create_inspect_tasks"] is True
    assert payload["data"]["allow_import_plan_tasks"] is True
    assert payload["data"]["max_steps"] == 5
    assert payload["data"]["failure_threshold"] == 3


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

    advanced = advance_agent_phase(tmp_path, "plan")
    assert advanced["current_phase"] == "plan"
    assert len(advanced["phase_history"]) == 2
    assert advanced["phase_history"][0]["phase"] == "intake"
    assert advanced["phase_history"][0]["exited_at"] is not None
    assert advanced["phase_history"][1]["phase"] == "plan"
    assert advanced["phase_history"][1]["exited_at"] is None

    session = get_agent_session(tmp_path)
    assert session["current_phase"] == "plan"
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
    start_workflow(project_path, mode="plan", session_id="s-cli", current_phase="drafting")

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
    assert mode_state["mode"] == "plan"
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


def test_workflow_status_human_output_renders_structured_agent_actions(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    create_agent_session(project_path, goal="结构化 action")
    update_agent_session(
        project_path,
        next_actions=[
            {
                "id": "import_tasks",
                "label": "导入任务",
                "risk": "medium",
                "suggested_command": "codepilot add -p demo -f tasks.json",
            }
        ],
    )

    result = CliRunner().invoke(main, ["workflow", "status", "-p", "demo"])

    assert result.exit_code == 0, result.output
    assert "导入任务" in result.output
    assert "import_tasks" in result.output


def test_workflow_next_list_returns_latest_next_actions_json(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)

    plan = CliRunner().invoke(main, ["plan", "-p", "demo", "改进 doctor", "--json", "--no-wiki"])
    assert plan.exit_code == 0, plan.output

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "workflow next"
    data = payload["data"]
    assert data["project"] == "demo"
    assert data["source"]["mode"] == "plan"
    assert any(action["id"] == "import_tasks" for action in data["next_actions"])
    assert db.get_task_stats("demo")["total"] == 0
    assert Path(data["source"]["context_path"]).is_relative_to(Path(project["path"]))


def test_workflow_next_executes_import_tasks_from_plan_without_shelling_suggested_command(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    plan = CliRunner().invoke(main, ["plan", "-p", "demo", "改进 doctor", "--json", "--no-wiki"])
    assert plan.exit_code == 0, plan.output
    plan_data = json.loads(plan.output)["data"]

    context_path = Path(plan_data["context_path"])
    context = json.loads(context_path.read_text(encoding="utf-8"))
    for action in context["next_actions"]:
        if action["id"] == "import_tasks":
            action["suggested_command"] = r"codepilot add -p demo -f C:\does-not-exist\tasks.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = read_workflow_state(project["path"], mode="plan")
    poisoned_actions = []
    for action in state["next_actions"]:
        cloned = dict(action)
        if cloned["id"] == "import_tasks":
            cloned["suggested_command"] = r"codepilot add -p demo -f C:\does-not-exist\tasks.json"
        poisoned_actions.append(cloned)
    update_workflow_state(project["path"], "plan", next_actions=poisoned_actions)

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", "import_tasks", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["action"]["id"] == "import_tasks"
    assert data["result"]["task_batch_path"] == plan_data["task_batch_path"]
    assert data["result"]["count"] == len(plan_data["task_candidates"])
    assert len(db.list_tasks(project="demo")) == len(plan_data["task_candidates"])

    consumed_state = read_workflow_state(project["path"], mode="plan")
    assert any(item["id"] == "import_tasks" for item in consumed_state["consumed_actions"])


def test_workflow_next_executes_import_tasks_from_task_batch_not_suggested_command(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    plan = CliRunner().invoke(main, ["plan", "-p", "demo", "新增 explore", "--json"])
    assert plan.exit_code == 0, plan.output
    plan_data = json.loads(plan.output)["data"]

    context_path = Path(plan_data["context_path"])
    context = json.loads(context_path.read_text(encoding="utf-8"))
    for action in context["next_actions"]:
        if action["id"] == "import_tasks":
            action["suggested_command"] = r"codepilot add -p demo -f C:\does-not-exist\tasks.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = read_workflow_state(project["path"], mode="plan")
    poisoned_actions = []
    for action in state["next_actions"]:
        cloned = dict(action)
        if cloned["id"] == "import_tasks":
            cloned["suggested_command"] = r"codepilot add -p demo -f C:\does-not-exist\tasks.json"
        poisoned_actions.append(cloned)
    update_workflow_state(project["path"], "plan", next_actions=poisoned_actions)

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", "import_tasks", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["action"]["id"] == "import_tasks"
    assert data["result"]["task_batch_path"] == plan_data["task_batch_path"]
    assert data["result"]["count"] == len(plan_data["task_candidates"])
    assert len(db.list_tasks(project="demo")) == len(plan_data["task_candidates"])

    consumed_state = read_workflow_state(project["path"], mode="plan")
    assert any(item["id"] == "import_tasks" for item in consumed_state["consumed_actions"])
    consumed_context = json.loads(context_path.read_text(encoding="utf-8"))
    assert any(item["id"] == "import_tasks" for item in consumed_context["consumed_actions"])

    listed = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--list", "--json"])
    assert listed.exit_code == 0, listed.output
    listed_ids = [item["id"] for item in json.loads(listed.output)["data"]["next_actions"]]
    assert "import_tasks" not in listed_ids
    assert "execute_directly" not in listed_ids

    status = CliRunner().invoke(main, ["workflow", "status", "-p", "demo", "--json"])
    assert status.exit_code == 0, status.output
    status_data = json.loads(status.output)["data"]
    state_ids = [item["id"] for item in status_data["state"]["next_actions"]]
    session_ids = [item["id"] for item in status_data["agent_session"]["next_action_details"]]
    assert "import_tasks" not in state_ids
    assert "execute_directly" not in state_ids
    assert "import_tasks" not in session_ids
    assert "execute_directly" not in session_ids
    phase_state_ids = [
        item["id"]
        for entry in status_data["agent_session"]["phase_history"]
        for item in (entry.get("mode_state") or {}).get("next_actions") or []
        if isinstance(item, dict)
    ]
    assert "import_tasks" not in phase_state_ids
    assert "execute_directly" not in phase_state_ids


def test_workflow_next_rejects_unknown_and_high_risk_actions_by_default(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    project_path = Path(project["path"])
    context_path = project_path / ".codepilot" / "context" / "unknown.json"
    context_path.parent.mkdir(parents=True)
    context_path.write_text(
        json.dumps(
            {
                "next_actions": [
                    {
                        "id": "unknown_action",
                        "label": "未知动作",
                        "risk": "low",
                        "suggested_command": "codepilot run -p demo --once",
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    start_workflow(project_path, mode="plan", session_id="unknown", context_path=context_path)
    update_workflow_state(
        project_path,
        "plan",
        next_actions=[
            {
                "id": "unknown_action",
                "label": "未知动作",
                "risk": "low",
                "suggested_command": "codepilot run -p demo --once",
            }
        ],
    )

    unknown = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", "unknown_action", "--json"])
    assert unknown.exit_code != 0
    unknown_payload = json.loads(unknown.output)
    assert unknown_payload["ok"] is False
    assert "不支持" in unknown_payload["error"]["message"]

    plan = CliRunner().invoke(main, ["plan", "-p", "demo", "新增 explore", "--json"])
    assert plan.exit_code == 0, plan.output

    high_risk = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", "execute_directly", "--json"])
    assert high_risk.exit_code != 0
    high_risk_payload = json.loads(high_risk.output)
    assert high_risk_payload["ok"] is False
    assert "高风险" in high_risk_payload["error"]["message"]


def test_inspect_candidate_id_normalizes_absolute_project_paths(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    project_path = Path(project["path"])
    source_file = project_path / "src" / "foo.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("def handle_timeout():\n    pass\n", encoding="utf-8")

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    base_candidate = {
        "title": "修复 foo.py 超时 TODO",
        "goal": "处理 foo.py 的超时 TODO。",
        "priority": "P3",
        "kind": "bug",
        "reason": "actionable",
        "evidence": "signal 3: src/foo.py:1 TODO handle timeout",
        "acceptance_criteria": ["TODO 已处理。"],
        "verification_commands": ["git diff --check"],
        "effort": "small",
    }
    base_result = {
        "project": "demo",
        "report_only": [],
        "dropped": [],
        "skipped": [],
        "quality_summary": {"created_count": 1, "report_only_count": 0},
    }
    absolute_result = {**base_result, "created": [dict(base_candidate, files=[str(source_file)])]}
    relative_result = {**base_result, "created": [dict(base_candidate, files=["src/foo.py"])]}

    absolute_context = write_inspect_workflow_context(
        project,
        absolute_result,
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-absolute",
    )
    relative_context = write_inspect_workflow_context(
        project,
        relative_result,
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-relative",
    )

    absolute_payload = json.loads(Path(absolute_context["context_path"]).read_text(encoding="utf-8"))
    relative_payload = json.loads(Path(relative_context["context_path"]).read_text(encoding="utf-8"))

    assert absolute_payload["created_preview"][0]["candidate_id"] == relative_payload["created_preview"][0]["candidate_id"]


def test_workflow_next_executes_inspect_create_tasks_without_shelling_suggested_command(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    project_path = Path(project["path"])
    (project_path / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    context = write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [
                {
                    "candidate_id": "inspect-actionable",
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py:1 的 TODO，避免超时路径继续缺实现。",
                    "priority": "P3",
                    "rationale": "TODO 指向明确文件。",
                    "kind": "bug",
                    "evidence": "signal 3: foo.py:1 TODO handle timeout",
                    "files": ["foo.py"],
                    "acceptance_criteria": ["foo.py:1 的超时 TODO 已处理。"],
                    "verification_commands": ["pytest tests/test_workflow_state.py -q"],
                    "effort": "small",
                }
            ],
            "report_only": [],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 1, "report_only_count": 0},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-test-create",
    )
    context_path = Path(context["context_path"])
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    for action in payload["next_actions"]:
        if action["id"] == "create_inspect_tasks":
            action["suggested_command"] = "codepilot run -p demo --once --json"
    context_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", "create_inspect_tasks", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["ok"] is True
    assert out["data"]["action"]["id"] == "create_inspect_tasks"
    assert out["data"]["result"]["created_count"] == 1
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 1
    assert tasks[0]["source"] == "inspector"
    assert "修复 foo.py 超时 TODO" == tasks[0]["title"]
    events = read_memory_events(project)
    assert any(
        event["event_type"] == "workflow.action_executed"
        and event["details"].get("action_id") == "create_inspect_tasks"
        for event in events
    )


def test_workflow_next_promotes_single_inspect_report_candidate(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    project_path = Path(project["path"])
    (project_path / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    context = write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [],
            "report_only": [
                {
                    "candidate_id": "inspect-report",
                    "title": "记录 foo.py P4 线索",
                    "goal": "记录 foo.py:1 的 TODO，后续人工判断是否需要整理。",
                    "priority": "P4",
                    "rationale": "TODO 只有低优先级整理价值。",
                    "kind": "chore",
                    "evidence": "signal 3: foo.py:1 TODO handle timeout",
                    "files": ["foo.py"],
                    "acceptance_criteria": ["foo.py:1 的 TODO 已被人工复核。"],
                    "verification_commands": ["git diff --check"],
                    "effort": "small",
                    "reason": "priority_p4_report_only",
                }
            ],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 0, "report_only_count": 1},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-test-promote",
    )

    action_id = next(
        action["id"]
        for action in context["next_actions"]
        if action["id"].startswith("promote_inspect_report_")
    )
    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", action_id, "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["data"]["result"]["created_count"] == 1
    task = db.list_tasks(project="demo")[0]
    assert task["title"] == "记录 foo.py P4 线索"
    assert task["priority"] == "P3"


def test_workflow_next_ignores_inspect_report_candidate_without_creating_task(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    project_path = Path(project["path"])
    (project_path / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    context = write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [],
            "report_only": [
                {
                    "candidate_id": "inspect-report",
                    "title": "记录 foo.py P4 线索",
                    "goal": "记录 foo.py:1 的 TODO，后续人工判断是否需要整理。",
                    "priority": "P4",
                    "rationale": "TODO 只有低优先级整理价值。",
                    "kind": "chore",
                    "evidence": "signal 3: foo.py:1 TODO handle timeout",
                    "files": ["foo.py"],
                    "acceptance_criteria": ["foo.py:1 的 TODO 已被人工复核。"],
                    "verification_commands": ["git diff --check"],
                    "effort": "small",
                    "reason": "priority_p4_report_only",
                }
            ],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 0, "report_only_count": 1},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-test-ignore",
    )

    action_id = next(action["id"] for action in context["next_actions"] if action["id"].startswith("ignore_inspect_report_"))
    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", action_id, "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["data"]["result"]["status"] == "ignored"
    assert out["data"]["result"]["candidate_id"] == "inspect-report"
    assert db.list_tasks(project="demo") == []

    refreshed = json.loads(Path(context["context_path"]).read_text(encoding="utf-8"))
    assert refreshed["report_only"] == []
    assert refreshed["ignored_report_only"][0]["candidate_id"] == "inspect-report"
    assert all("inspect-report" not in action["id"] for action in refreshed["next_actions"])

    feedback_events = read_memory_events(project, event_type="inspect.report_feedback")
    assert feedback_events[0]["details"]["candidate_id"] == "inspect-report"
    assert feedback_events[0]["details"]["status"] == "ignored"
    feedback_candidates = [item for item in read_memory_candidates(project) if item["event_type"] == "inspect.report_feedback"]
    assert feedback_candidates[0]["feedback"] == "negative"


def test_workflow_next_auto_ignores_repeated_negative_report_only(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context
    from codepilot.core.memory import append_memory_event

    append_memory_event(
        project,
        event_type="inspect.report_feedback",
        source="codepilot.inspect",
        summary="巡检报告项ignored：记录 foo.py P4 线索",
        details={
            "action_id": "ignore_inspect_report_inspect-report",
            "candidate_id": "inspect-report",
            "status": "ignored",
            "title": "记录 foo.py P4 线索",
            "reason": "priority_p4_report_only",
            "files": ["foo.py"],
        },
        tags=["inspect", "feedback", "ignored"],
    )
    context = write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [],
            "report_only": [
                {
                    "candidate_id": "inspect-report",
                    "title": "记录 foo.py P4 线索",
                    "goal": "人工评估 foo.py。",
                    "priority": "P4",
                    "reason": "priority_p4_report_only",
                    "files": ["foo.py"],
                    "evidence": "signal 3: foo.py",
                }
            ],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 0, "report_only_count": 1},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-auto-ignore",
    )

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--auto", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["data"]["auto"] is True
    assert out["data"]["selected_reason"] == "negative_feedback_report_only"
    assert out["data"]["action"]["id"] == "ignore_inspect_report_inspect-report"
    assert db.list_tasks(project="demo") == []
    refreshed = json.loads(Path(context["context_path"]).read_text(encoding="utf-8"))
    assert refreshed["report_only"] == []
    assert refreshed["ignored_report_only"][0]["candidate_id"] == "inspect-report"


def test_workflow_next_auto_generates_inspect_plan_once(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [
                {
                    "candidate_id": "inspect-actionable",
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py 中的超时 TODO。",
                    "priority": "P2",
                    "reason": "todo_signal",
                    "files": ["foo.py"],
                    "evidence": "signal 1: foo.py",
                }
            ],
            "report_only": [],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 1, "report_only_count": 0},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-auto-plan",
    )

    first = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--auto", "--json"])
    second = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--auto", "--json"])

    assert first.exit_code == 0, first.output
    first_out = json.loads(first.output)
    assert first_out["data"]["auto"] is True
    assert first_out["data"]["selected_reason"] == "low_risk_inspect_plan"
    assert first_out["data"]["action"]["id"] == "plan_from_inspect"
    assert db.list_tasks(project="demo") == []

    assert second.exit_code == 0, second.output
    second_out = json.loads(second.output)
    assert second_out["data"]["auto"] is True
    assert second_out["data"]["action"] is None
    assert second_out["data"]["skipped_reason"] == "no_low_risk_auto_action"


def test_workflow_next_auto_does_not_import_plan_tasks_by_default(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    plan = CliRunner().invoke(main, ["plan", "-p", "demo", "新增 explore", "--json"])
    assert plan.exit_code == 0, plan.output
    plan_data = json.loads(plan.output)["data"]

    context_path = Path(plan_data["context_path"])
    context = json.loads(context_path.read_text(encoding="utf-8"))
    for action in context["next_actions"]:
        if action["id"] == "import_tasks":
            action["suggested_command"] = r"codepilot add -p demo -f C:\does-not-exist\tasks.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--auto", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["data"]["auto"] is True
    assert out["data"]["action"] is None
    assert out["data"]["skipped_reason"] == "no_low_risk_auto_action"
    assert out["data"]["policy"]["allow_import_plan_tasks"] is False
    assert db.list_tasks(project="demo") == []
    assert Path(project["path"]).exists()


def test_workflow_next_auto_imports_plan_tasks_when_enabled_without_shelling_suggested_command(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    (Path(project["path"]) / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[automation]
workflow_auto_import_plan_tasks = true
workflow_auto_max_steps = 1
""".strip(),
        encoding="utf-8",
    )
    plan = CliRunner().invoke(main, ["plan", "-p", "demo", "新增 explore", "--json"])
    assert plan.exit_code == 0, plan.output
    plan_data = json.loads(plan.output)["data"]

    context_path = Path(plan_data["context_path"])
    context = json.loads(context_path.read_text(encoding="utf-8"))
    for action in context["next_actions"]:
        if action["id"] == "import_tasks":
            action["suggested_command"] = r"codepilot add -p demo -f C:\does-not-exist\tasks.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--auto", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["data"]["selected_reason"] == "policy_allowed_plan_import"
    assert out["data"]["action"]["id"] == "import_tasks"
    assert out["data"]["result"]["task_batch_path"] == plan_data["task_batch_path"]
    assert len(db.list_tasks(project="demo")) == len(plan_data["task_candidates"])


def test_workflow_next_auto_chains_plan_and_import_until_configured_step_limit(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    (Path(project["path"]) / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[automation]
workflow_auto_import_plan_tasks = true
workflow_auto_max_steps = 2
""".strip(),
        encoding="utf-8",
    )

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [
                {
                    "candidate_id": "inspect-actionable",
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py 中的超时 TODO。",
                    "priority": "P2",
                    "reason": "todo_signal",
                    "files": ["foo.py"],
                    "evidence": "signal 1: foo.py",
                }
            ],
            "report_only": [],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 1, "report_only_count": 0},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-auto-chain",
    )

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--auto", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert [step["action"]["id"] for step in data["steps"]] == ["plan_from_inspect", "import_tasks"]
    assert [step["selected_reason"] for step in data["steps"]] == [
        "low_risk_inspect_plan",
        "policy_allowed_plan_import",
    ]
    assert data["stopped_reason"] == "max_steps_reached"
    assert db.list_tasks(project="demo")


def test_workflow_next_auto_creates_inspect_tasks_when_enabled(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    (Path(project["path"]) / "foo.py").write_text("def handle_timeout():\n    pass\n", encoding="utf-8")
    (Path(project["path"]) / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[automation]
workflow_auto_create_inspect_tasks = true
workflow_auto_max_steps = 1
""".strip(),
        encoding="utf-8",
    )

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [
                {
                    "candidate_id": "inspect-actionable",
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py 中的超时 TODO。",
                    "priority": "P2",
                    "reason": "todo_signal",
                    "files": ["foo.py"],
                    "evidence": "signal 1: foo.py",
                }
            ],
            "report_only": [],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 1, "report_only_count": 0},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-auto-create",
    )

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--auto", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["selected_reason"] == "policy_allowed_inspect_task_creation"
    assert data["action"]["id"] == "create_inspect_tasks"
    assert data["result"]["created_count"] == 1
    assert db.list_tasks(project="demo")[0]["source"] == "inspector"


def test_workflow_next_auto_opens_failure_circuit_at_configured_threshold(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    (Path(project["path"]) / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[automation]
workflow_auto_max_steps = 3
workflow_auto_failure_threshold = 1
""".strip(),
        encoding="utf-8",
    )

    from codepilot.commands import workflow as workflow_cmd
    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [
                {
                    "candidate_id": "inspect-actionable",
                    "title": "修复 foo.py 超时 TODO",
                    "goal": "处理 foo.py 中的超时 TODO。",
                    "priority": "P2",
                    "reason": "todo_signal",
                    "files": ["foo.py"],
                    "evidence": "signal 1: foo.py",
                }
            ],
            "report_only": [],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 1, "report_only_count": 0},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-auto-fail",
    )

    def fail_execute(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(workflow_cmd, "_execute_next_action", fail_execute)

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--auto", "--json"])

    assert result.exit_code != 0
    out = json.loads(result.output)
    assert out["ok"] is False
    assert "自动推进失败达到熔断阈值" in out["error"]["message"]


def test_workflow_next_builds_plan_from_inspect_context(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)

    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    context = write_inspect_workflow_context(
        project,
        {
            "project": "demo",
            "created": [],
            "report_only": [],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 0, "report_only_count": 0},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-test-plan",
    )

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", "plan_from_inspect", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    plan_result = out["data"]["result"]
    assert plan_result["source"] == "inspect"
    assert plan_result["source_path"] == context["context_path"]
    assert Path(plan_result["plan_path"]).is_file()
