"""Go/auto clarification flow tests."""

from __future__ import annotations

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from tests.chat_flow_testkit import register_project


def test_go_runs_interactive_clarification_before_planning(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    def fake_clarify(title, *, project_info, qa_history=None, planner="codex", **kw):
        if qa_history:
            answer = qa_history[-1].get("answer", "")
            return {
                "status": "ready",
                "refined_title": f"{title} / 补充: {answer}",
                "qa_history": qa_history,
            }
        return {
            "status": "needs_clarification",
            "questions": ["先优先做哪一块?"],
            "qa_history": [],
            "turn": 1,
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)

    captured = {}

    def fake_run_requirement_workflow(**kwargs):
        captured["title"] = kwargs["title"]
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(auto_mod, "run_requirement_workflow", fake_run_requirement_workflow)
    original_get_stream = auto_mod.click.get_text_stream

    class _TtyIn:
        @staticmethod
        def isatty():
            return True

    monkeypatch.setattr(
        auto_mod.click,
        "get_text_stream",
        lambda name: _TtyIn() if name == "stdin" else original_get_stream(name),
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["go", "做一个全自动编程工作流智能体", "--execute"],
        input="先打通规划-执行闭环\n",
    )

    assert result.exit_code == 0
    assert "先补充几个关键信息" in result.output
    assert "补充: 先打通规划-执行闭环" in captured["title"]


def test_auto_command_runs_interactive_clarification_before_planning(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    def fake_clarify(title, *, project_info, qa_history=None, planner="codex", **kw):
        if qa_history:
            return {
                "status": "ready",
                "refined_title": title + " / 补充: " + qa_history[-1]["answer"],
                "qa_history": qa_history,
            }
        return {
            "status": "needs_clarification",
            "questions": ["先落哪个入口?"],
            "qa_history": [],
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)

    captured = {}

    def fake_run_requirement_workflow(**kwargs):
        captured["title"] = kwargs["title"]
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(auto_mod, "run_requirement_workflow", fake_run_requirement_workflow)
    original_get_stream = auto_mod.click.get_text_stream

    class _TtyIn:
        @staticmethod
        def isatty():
            return True

    monkeypatch.setattr(
        auto_mod.click,
        "get_text_stream",
        lambda name: _TtyIn() if name == "stdin" else original_get_stream(name),
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["auto", "-p", "demo", "-t", "做一个全自动编程工作流智能体", "--plan-only"],
        input="先落 Web UI 需求入口\n",
    )

    assert result.exit_code == 0
    assert "先补充几个关键信息" in result.output
    assert "先落 Web UI 需求入口" in captured["title"]


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
        "classify_intent",
        lambda text, **kw: {"intent": "task", "source": "forced"},
    )

    def fake_run_requirement_workflow(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(auto_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["go", "修复登录 bug", "--max-tasks", "9"])

    assert result.exit_code == 0
    assert captured["max_tasks"] == 1


def test_go_pending_clarification_error_does_not_fall_through_to_planning(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })
    monkeypatch.setattr(auto_mod, "clarify_requirement", lambda *a, **kw: {
        "status": "needs_clarification",
        "questions": ["先聚焦哪块?"],
        "qa_history": [],
    })
    monkeypatch.setattr(auto_mod, "continue_pending_clarification", lambda *a, **kw: {
        "status": "error",
        "error_kind": "runtime",
        "message": "clarifier backend exploded",
    })

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw["title"]))
    original_get_stream = auto_mod.click.get_text_stream

    class _TtyIn:
        @staticmethod
        def isatty():
            return True

    monkeypatch.setattr(
        auto_mod.click,
        "get_text_stream",
        lambda name: _TtyIn() if name == "stdin" else original_get_stream(name),
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["go", "优化一下"],
        input="先做 Web UI\n",
    )

    assert result.exit_code != 0
    assert "clarifier backend exploded" in result.output
    assert "Aborted!" not in result.output
    assert not ran
