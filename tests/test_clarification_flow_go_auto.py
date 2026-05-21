"""Go/auto clarification flow tests."""

from __future__ import annotations

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from tests.chat_flow_testkit import register_project


def _q(text: str, *, qid: str = "q1", qtype: str = "text", options: list[tuple[str, str]] | None = None, allow_free_text: bool = False) -> dict:
    return {
        "id": qid,
        "type": qtype,
        "text": text,
        "options": [{"id": option_id, "label": label} for option_id, label in (options or [])],
        "allow_free_text": allow_free_text,
    }


def test_go_bypasses_clarification_and_plans_directly(tmp_path, monkeypatch):
    """go 命令跳过旧的硬编码澄清，直接走规划（由 AI 智能体接管需求理解）。"""
    register_project(tmp_path, monkeypatch)

    captured = {}

    def fake_run_requirement_workflow(**kwargs):
        captured["title"] = kwargs["title"]
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(auto_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["go", "做一个全自动编程工作流智能体"])

    assert result.exit_code == 0
    assert captured["title"] == "做一个全自动编程工作流智能体"


def test_auto_command_bypasses_clarification_and_plans_directly(tmp_path, monkeypatch):
    """auto 命令跳过旧的硬编码澄清，直接走规划。"""
    register_project(tmp_path, monkeypatch)

    captured = {}

    def fake_run_requirement_workflow(**kwargs):
        captured["title"] = kwargs["title"]
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(auto_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["auto", "-p", "demo", "-t", "做一个全自动编程工作流智能体", "--plan-only"],
    )

    assert result.exit_code == 0
    assert captured["title"] == "做一个全自动编程工作流智能体"


def test_go_routes_command_intent_to_guidance_without_planning(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    ran: list[dict] = []
    monkeypatch.setattr(
        auto_mod,
        "classify_intent",
        lambda text, **kw: {"intent": "command", "source": "forced"},
    )
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw))

    runner = CliRunner()
    result = runner.invoke(main, ["go", "查看状态"])

    assert result.exit_code == 0
    assert "codepilot status" in result.output
    assert not ran


def test_go_task_intent_forces_single_task_planning(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    captured: dict = {}
    monkeypatch.setattr(
        auto_mod,
        "classify_entry_intent",
        lambda text, **kw: "task",
    )

    def fake_run_requirement_workflow(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(auto_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["go", "修复登录 bug", "--max-tasks", "9"])

    assert result.exit_code == 0
    assert captured["max_tasks"] == 1


def test_go_rejects_removed_legacy_classifier_option(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["go", "--legacy-classifier", "修复登录 bug"])

    assert result.exit_code != 0
    assert "No such option: --legacy-classifier" in result.output


def test_root_rejects_removed_legacy_classifier_option():
    result = CliRunner().invoke(main, ["--legacy-classifier", "--help"])

    assert result.exit_code != 0
    assert "No such option: --legacy-classifier" in result.output


def test_go_question_intent_answers_directly_without_planning(tmp_path, monkeypatch):
    """go 命令识别到 question intent 时直接回答，不走规划。"""
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_entry_intent", lambda text, **kw: "question")
    monkeypatch.setattr(auto_mod, "answer_question_via_api", lambda **kw: "这是回答")

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw))

    runner = CliRunner()
    result = runner.invoke(main, ["go", "当前项目进展如何"])

    assert result.exit_code == 0
    assert "这是回答" in result.output
    assert not ran


def test_assess_requirement_returns_workflow_session(tmp_path, monkeypatch):
    """assess_requirement_for_planning 返回 workflow_session 记录。"""
    from codepilot.commands.auto_workflow import assess_requirement_for_planning
    from tests.workflow_testkit import init_test_db as _init_test_db
    from codepilot.storage import database as db

    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project_info = db.register_project("demo", str(project_path))

    seen: dict[str, object] = {}

    def _fake_clarify(title, *, qa_history=None, **kwargs):
        seen["title"] = title
        seen["qa_history"] = qa_history
        return {"status": "ready", "refined_title": title, "qa_history": qa_history, "source": "passthrough", "skip_reason": "delegated_to_ai_agent"}

    monkeypatch.setattr(auto_mod, "clarify_requirement", _fake_clarify)

    result = assess_requirement_for_planning(
        "做一个智能体",
        project_info=project_info,
        planner="codex",
    )

    assert result["status"] == "ready"
    ws = result.get("workflow_session")
    assert ws is not None, "workflow_session should be present in ready result"
    assert ws["phase"] == "plan"
    assert ws["intent"] == "requirement"
    assert ws["next_action"] == "plan"
    assert ws["clarify_status"]["status"] == "ready"
    assert ws["clarify_status"]["source"] == "passthrough"
    assert ws["clarify_status"]["skip_reason"] == "delegated_to_ai_agent"
