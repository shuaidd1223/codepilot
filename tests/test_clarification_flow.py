"""End-to-end-ish tests for the clarification flow in chat + Web UI."""

from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot import db
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from codepilot.commands import auto_workflow as auto_workflow_mod


def _init_test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    db.init_db()
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0


def _register_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "README.md").write_text("# Demo", encoding="utf-8")
    db.register_project("demo", str(project_path))
    return project_path


def test_chat_asks_for_clarification_then_plans(tmp_path, monkeypatch):
    """A vague requirement triggers clarification; after user answers, planning runs."""
    _register_project(tmp_path, monkeypatch)

    # Force intent classifier to 'requirement' regardless.
    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })

    # Stub clarify_requirement: ask once, then ready.
    calls = {"count": 0}

    def fake_clarify(title, *, project_info, qa_history=None, planner="codex", **kw):
        calls["count"] += 1
        if not qa_history:
            return {
                "status": "needs_clarification",
                "questions": ["优化哪一块?"],
                "qa_history": [],
                "turn": 1,
            }
        return {
            "status": "ready",
            "refined_title": title + " -> webui 启动速度",
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
        input="优化一下\nwebui 启动速度\n/exit\n",
    )

    assert result.exit_code == 0
    # Clarify was called twice (before + after the user answer).
    assert calls["count"] >= 2
    # Planner ran once with the refined title.
    assert captured, "run_requirement_workflow should have been invoked"
    assert "webui" in captured[0]


def test_chat_slash_clear_aborts_pending_clarification(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })
    monkeypatch.setattr(auto_mod, "clarify_requirement", lambda *a, **kw: {
        "status": "needs_clarification",
        "questions": ["哪一块?"],
        "qa_history": [],
        "turn": 1,
    })

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw["title"]))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui"],
        input="优化\n/clear\n/exit\n",
    )
    assert result.exit_code == 0
    assert not ran, "planner should not run after /clear during clarification"


def test_webui_submit_goal_returns_clarify(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(
        "codepilot.webui_actions.clarify_requirement",
        lambda *a, **kw: {
            "status": "needs_clarification",
            "questions": ["要优化哪里?"],
            "qa_history": [],
        },
    )

    out = webui_mod.submit_goal_action("demo", "优化一下", category="requirement")
    assert out["intent"] == "clarify"
    assert out["questions"] == ["要优化哪里?"]
    assert out["original_title"] == "优化一下"


def test_webui_submit_goal_continues_with_history(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

    def fake_clarify(title, *, project_info, qa_history=None, planner="codex", **kw):
        if qa_history and len(qa_history) >= 1:
            return {"status": "ready", "refined_title": title + " refined", "qa_history": qa_history}
        return {"status": "needs_clarification", "questions": ["?"], "qa_history": []}

    monkeypatch.setattr("codepilot.webui_actions.clarify_requirement", fake_clarify)

    plan_calls = []

    def fake_submit(project, text, **kw):
        plan_calls.append(text)
        return {"ok": True, "message": "queued", "job": {"task_ids": []}}

    monkeypatch.setattr(
        "codepilot.webui_actions.submit_requirement_action", fake_submit
    )

    out = webui_mod.submit_goal_action(
        "demo",
        "webui 启动速度",
        category="requirement",
        qa_history=[],
        original_title="优化一下",
    )
    assert out["intent"] == "requirement"
    assert "refined" in out.get("refined_title", "")
    assert plan_calls
