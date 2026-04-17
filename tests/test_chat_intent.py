"""Integration tests for chat intent routing (mock AI, real DB + CLI)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot import __version__
from codepilot import db
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import auto as auto_mod


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


# ─── heuristic routing in chat ───────────────────────────────────────────────

def test_chat_question_heuristic_does_not_create_task(tmp_path, monkeypatch):
    """A question like '怎么用' should not create any task."""
    _register_project(tmp_path, monkeypatch)

    # Mock answer_question_via_api to return a canned answer
    monkeypatch.setattr(
        auto_mod,
        "answer_question_via_api",
        lambda **kwargs: "CodePilot 是一个工作流工具。",
    )
    # Stub out classify_intent to use heuristic only (no AI call)
    original_classify = auto_mod.classify_intent

    def _heuristic_only(text, **kwargs):
        from codepilot.ai import _heuristic_intent
        intent = _heuristic_intent(text)
        if intent:
            return {"intent": intent, "reason": "heuristic", "source": "heuristic"}
        return {"intent": "requirement", "reason": "default", "source": "default"}

    monkeypatch.setattr(auto_mod, "classify_intent", _heuristic_only)

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="怎么用这个工具\n/exit\n")

    assert result.exit_code == 0
    assert "CodePilot 是一个工作流工具" in result.output
    # Should NOT have created any task
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 0


def test_chat_requirement_heuristic_triggers_planning(tmp_path, monkeypatch):
    """A requirement like '帮我修复 bug' should trigger the planning flow."""
    _register_project(tmp_path, monkeypatch)

    planning_called = {"count": 0}

    def _mock_breakdown(**kwargs):
        planning_called["count"] += 1
        return {
            "summary": "修复 bug",
            "complexity": "simple",
            "should_split": False,
            "tasks": [{"title": "修复 bug", "priority": "P2"}],
        }

    monkeypatch.setattr(auto_mod, "generate_task_breakdown", _mock_breakdown)
    monkeypatch.setattr(auto_mod, "run_backlog", lambda *a, **kw: {
        "processed": 0, "done": 0, "failed": 0, "requeued": 0, "cancelled": 0,
    })

    def _heuristic_only(text, **kwargs):
        from codepilot.ai import _heuristic_intent
        intent = _heuristic_intent(text)
        if intent:
            return {"intent": intent, "reason": "heuristic", "source": "heuristic"}
        return {"intent": "requirement", "reason": "default", "source": "default"}

    monkeypatch.setattr(auto_mod, "classify_intent", _heuristic_only)

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="帮我修复登录 bug\n/exit\n")

    assert result.exit_code == 0
    assert planning_called["count"] >= 1, "Planning should have been triggered"


def test_chat_command_heuristic_shows_help(tmp_path, monkeypatch):
    """A command like '查看状态' should show CLI help, not create task."""
    _register_project(tmp_path, monkeypatch)

    def _heuristic_only(text, **kwargs):
        from codepilot.ai import _heuristic_intent
        intent = _heuristic_intent(text)
        if intent:
            return {"intent": intent, "reason": "heuristic", "source": "heuristic"}
        return {"intent": "requirement", "reason": "default", "source": "default"}

    monkeypatch.setattr(auto_mod, "classify_intent", _heuristic_only)

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="查看状态\n/exit\n")

    assert result.exit_code == 0
    assert "codepilot status" in result.output
    # No task created
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 0


def test_chat_slash_version_shows_current_version_without_creating_task(tmp_path, monkeypatch):
    """The interactive chat command dispatcher should handle /version locally."""
    _register_project(tmp_path, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="/version\n/exit\n")

    assert result.exit_code == 0
    assert f"CodePilot {__version__}" in result.output
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 0


# ─── classify_intent fallback chain ──────────────────────────────────────────

def test_classify_intent_uses_heuristic_first(monkeypatch):
    """Heuristic match should short-circuit without calling any AI."""
    from codepilot.ai import classify_intent

    result = classify_intent("怎么安装这个工具")
    assert result["intent"] == "question"
    assert result["source"] == "heuristic"


def test_classify_intent_defaults_to_requirement_on_failure(monkeypatch):
    """When heuristic misses and all AI fails, default to requirement."""
    from codepilot.ai import classify_intent
    import codepilot.ai as ai_mod

    # Force heuristic to miss
    monkeypatch.setattr(ai_mod, "_heuristic_intent", lambda t: None)
    # Force codex classifier to fail
    monkeypatch.setattr(ai_mod, "_classify_via_codex", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no codex")))

    result = classify_intent("some ambiguous input")
    assert result["intent"] == "requirement"
    assert result["source"] == "default"
