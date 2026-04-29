"""Chat clarification flow tests."""

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


def test_chat_asks_for_clarification_then_plans(tmp_path, monkeypatch):
    """A vague requirement triggers clarification; after user answers, planning runs."""
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })

    calls = {"count": 0}

    def fake_clarify(title, *, project_info, qa_history=None, planner="codex", **kw):
        calls["count"] += 1
        if not qa_history:
            return {
                "status": "needs_clarification",
                "questions": [
                    _q(
                        "优化哪一块?",
                        qid="scope",
                        qtype="single",
                        options=[("web", "Web UI"), ("cli", "CLI")],
                        allow_free_text=True,
                    )
                ],
                "qa_history": [],
                "turn": 1,
            }
        return {
            "status": "ready",
            "refined_title": title + " -> " + qa_history[-1]["answer"],
            "qa_history": qa_history,
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)

    captured = []

    def fake_run_requirement_workflow(**kwargs):
        captured.append(kwargs["title"])
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(auto_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui"],
        input="# 优化一下\n1\n/exit\n",
    )

    assert result.exit_code == 0
    assert calls["count"] >= 2
    assert captured, "run_requirement_workflow should have been invoked"
    assert "Web UI" in captured[0]


def test_chat_slash_clear_aborts_pending_clarification(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })
    monkeypatch.setattr(auto_mod, "clarify_requirement", lambda *a, **kw: {
        "status": "needs_clarification",
        "questions": [_q("哪一块?")],
        "qa_history": [],
        "turn": 1,
    })

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw["title"]))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui"],
        input="# 优化\n/clear\n/exit\n",
    )
    assert result.exit_code == 0
    assert not ran, "planner should not run after /clear during clarification"


def test_chat_pending_clarification_exception_does_not_crash(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })

    def fake_clarify(title, *, qa_history=None, **kw):
        if qa_history:
            raise RuntimeError("clarifier backend exploded")
        return {
            "status": "needs_clarification",
            "questions": [_q("先优化哪一块?")],
            "qa_history": [],
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw["title"]))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui"],
        input="# 优化一下\n先优化 Web UI\n/exit\n",
    )

    assert result.exit_code == 0
    assert "clarifier backend exploded" in result.output
    assert not ran, "planner should not run when clarification evaluation fails"


def test_chat_pending_clarification_interrupt_exits_cleanly(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })

    def fake_clarify(title, *, qa_history=None, **kw):
        if qa_history:
            raise KeyboardInterrupt()
        return {
            "status": "needs_clarification",
            "questions": [_q("先优化哪一块?")],
            "qa_history": [],
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw["title"]))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui"],
        input="# 优化一下\n先优化 Web UI\n",
    )

    assert result.exit_code == 0
    assert "会话已结束" in result.output
    assert "Aborted!" not in result.output
    assert not ran, "planner should not run after clarification is interrupted"


def test_chat_pending_clarification_keeps_task_intent_single_task_limit(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    classify_calls: list[str] = []

    def fake_classify(text, **kw):
        classify_calls.append(text)
        return {"intent": "task", "source": "forced"}

    monkeypatch.setattr(auto_mod, "classify_intent", fake_classify)

    def fake_clarify(title, *, qa_history=None, **kw):
        if qa_history:
            answer = qa_history[-1].get("answer", "")
            return {
                "status": "ready",
                "refined_title": f"{title} / 补充: {answer}",
                "qa_history": qa_history,
            }
        return {
            "status": "needs_clarification",
            "questions": [_q("先修哪一块?")],
            "qa_history": [],
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)

    captured: dict = {}

    def fake_run_requirement_workflow(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "tasks": []}

    monkeypatch.setattr(auto_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui", "--max-tasks", "9"],
        input="! 修复登录失败\n先修重试逻辑\n/exit\n",
    )

    assert result.exit_code == 0
    assert classify_calls == [], "explicit task prefix and pending clarification turns should skip classification"
    assert captured["max_tasks"] == 1
    assert "重试逻辑" in captured["title"]


def test_chat_pending_clarification_empty_answer_does_not_reenter_clarifier(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })

    clarify_calls = {"count": 0}

    def fake_clarify(title, *, qa_history=None, **kw):
        clarify_calls["count"] += 1
        return {
            "status": "needs_clarification",
            "questions": [_q("先优化哪一块?")],
            "qa_history": qa_history or [],
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)
    monkeypatch.setattr(auto_mod, "_prompt_clarification_answers_for_cli", lambda *a, **kw: ("empty", [], ""))

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw["title"]))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui"],
        input="# 优化一下\n继续\n/exit\n",
    )

    assert result.exit_code == 0
    assert "请至少回答一个澄清问题" in result.output
    assert clarify_calls["count"] == 1
    assert not ran
