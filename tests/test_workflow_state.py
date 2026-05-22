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
    read_workflow_state,
    start_workflow,
    update_agent_session,
    update_workflow_state,
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

    clarify = CliRunner().invoke(main, ["clarify", "-p", "demo", "改进 doctor", "--json"])
    assert clarify.exit_code == 0, clarify.output

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "workflow next"
    data = payload["data"]
    assert data["project"] == "demo"
    assert data["source"]["mode"] == "clarify"
    assert any(action["id"] == "plan_from_spec" for action in data["next_actions"])
    assert db.get_task_stats("demo")["total"] == 0
    assert Path(data["source"]["context_path"]).is_relative_to(Path(project["path"]))


def test_workflow_next_executes_plan_from_spec_without_shelling_suggested_command(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    clarify = CliRunner().invoke(main, ["clarify", "-p", "demo", "改进 doctor", "--json"])
    assert clarify.exit_code == 0, clarify.output
    clarify_data = json.loads(clarify.output)["data"]
    context_path = Path(clarify_data["context_path"])
    context = json.loads(context_path.read_text(encoding="utf-8"))
    for action in context["next_actions"]:
        if action["id"] == "plan_from_spec":
            action["suggested_command"] = "codepilot run -p demo --once --json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["workflow", "next", "-p", "demo", "--action", "plan_from_spec", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    data = payload["data"]
    plan_result = data["result"]
    assert data["action"]["id"] == "plan_from_spec"
    assert plan_result["source"] == "spec"
    assert plan_result["source_path"] == clarify_data["artifact_path"]
    assert Path(plan_result["plan_path"]).exists()
    assert Path(plan_result["plan_path"]).is_relative_to(Path(project["path"]))


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
