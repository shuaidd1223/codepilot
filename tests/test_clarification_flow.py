"""End-to-end-ish tests for the clarification flow in chat + Web UI."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from codepilot import db
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from codepilot.commands import auto_workflow as auto_workflow_mod


def _init_test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
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
    monkeypatch.chdir(project_path)
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


def test_chat_pending_clarification_exception_does_not_crash(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })

    def fake_clarify(title, *, qa_history=None, **kw):
        if qa_history:
            raise RuntimeError("clarifier backend exploded")
        return {
            "status": "needs_clarification",
            "questions": ["先优化哪一块?"],
            "qa_history": [],
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw["title"]))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui"],
        input="优化一下\n先优化 Web UI\n/exit\n",
    )

    assert result.exit_code == 0
    assert "clarifier backend exploded" in result.output
    assert not ran, "planner should not run when clarification evaluation fails"


def test_chat_pending_clarification_interrupt_exits_cleanly(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_intent", lambda text, **kw: {
        "intent": "requirement", "source": "forced",
    })

    def fake_clarify(title, *, qa_history=None, **kw):
        if qa_history:
            raise KeyboardInterrupt()
        return {
            "status": "needs_clarification",
            "questions": ["先优化哪一块?"],
            "qa_history": [],
        }

    monkeypatch.setattr(auto_mod, "clarify_requirement", fake_clarify)

    ran = []
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw["title"]))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "--no-ui"],
        input="优化一下\n先优化 Web UI\n",
    )

    assert result.exit_code == 0
    assert "会话已结束" in result.output
    assert "Aborted!" not in result.output
    assert not ran, "planner should not run after clarification is interrupted"


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


def test_go_runs_interactive_clarification_before_planning(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

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
    _register_project(tmp_path, monkeypatch)

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


def test_go_routes_question_intent_to_answer_without_planning(tmp_path, monkeypatch):
    project_path = _register_project(tmp_path, monkeypatch)

    captured: dict[str, dict] = {}

    def fake_classify(text, **kwargs):
        captured["classify"] = kwargs
        return {"intent": "question", "source": "forced"}

    def fake_answer(**kwargs):
        captured["answer"] = kwargs
        return "这是 go 的问答回复"

    ran: list[dict] = []
    monkeypatch.setattr(auto_mod, "classify_intent", fake_classify)
    monkeypatch.setattr(auto_mod, "answer_question_via_api", fake_answer)
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw))

    runner = CliRunner()
    result = runner.invoke(main, ["go", "这个工具怎么用"])

    assert result.exit_code == 0
    assert "这是 go 的问答回复" in result.output
    assert not ran
    classify_opts = captured["classify"]["gateway_options"]
    answer_opts = captured["answer"]["gateway_options"]
    assert classify_opts.config_ref == str(project_path)
    assert answer_opts.config_ref == str(project_path)
    assert classify_opts is answer_opts


def test_go_routes_command_intent_to_guidance_without_planning(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

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
    _register_project(tmp_path, monkeypatch)

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
    _register_project(tmp_path, monkeypatch)

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


def test_webui_submit_requirement_returns_clarify_before_job(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(
        "codepilot.webui_actions.clarify_requirement",
        lambda *a, **kw: {
            "status": "needs_clarification",
            "questions": ["先覆盖哪个入口?"],
            "qa_history": [],
        },
    )

    out = webui_mod.submit_requirement_action(
        "demo",
        "做一个全自动编程工作流智能体",
        run_async=False,
    )

    assert out["intent"] == "clarify"
    assert out["questions"] == ["先覆盖哪个入口?"]
    assert webui_mod.list_ui_jobs("demo") == []


def test_webui_submit_requirement_continues_after_clarify_answer(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)

    def fake_clarify(title, *, qa_history=None, **kw):
        if qa_history:
            return {
                "status": "ready",
                "refined_title": title + " / 补充: " + qa_history[-1]["answer"],
                "qa_history": qa_history,
            }
        return {
            "status": "needs_clarification",
            "questions": ["先覆盖哪个入口?"],
            "qa_history": [],
        }

    monkeypatch.setattr("codepilot.webui_actions.clarify_requirement", fake_clarify)

    def fake_run_requirement_workflow(**kwargs):
        task = db.create_task(
            kwargs["project_info"]["name"],
            kwargs["title"],
            agent="codex",
            project_path=kwargs["project_info"]["path"],
        )
        return {"summary": "ok", "tasks": [task], "run": {}}

    monkeypatch.setattr(webui_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    out = webui_mod.submit_requirement_action(
        "demo",
        "先覆盖 Web UI 需求入口",
        original_title="做一个全自动编程工作流智能体",
        qa_history=[],
        run_async=False,
    )

    assert out["ok"] is True
    assert out.get("intent") != "clarify"
    jobs = webui_mod.list_ui_jobs("demo")
    assert jobs and jobs[0]["task_ids"]
    task = db.get_task(jobs[0]["task_ids"][0])
    assert "先覆盖 Web UI 需求入口" in task["title"]


def test_webui_session_clarification_follows_project_planner(tmp_path, monkeypatch):
    project_path = _register_project(tmp_path, monkeypatch)
    config_root = tmp_path / "config-root"
    config_root.mkdir()
    config_file = config_root / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
[automation]
planner = "claude"
""".strip(),
        encoding="utf-8",
    )
    db.register_project("demo", str(project_path), config_file=str(config_file))
    session = webui_mod.create_session_action("demo", title="chat")

    clarify_planners: list[str] = []
    submit_planners: list[str] = []

    def fake_clarify(title, *, qa_history=None, planner="codex", **kw):
        clarify_planners.append(planner)
        if qa_history:
            return {"status": "ready", "refined_title": f"{title} refined", "qa_history": qa_history}
        return {"status": "needs_clarification", "questions": ["先做哪块?"], "qa_history": []}

    monkeypatch.setattr("codepilot.webui_actions.clarify_requirement", fake_clarify)

    def fake_submit(project, text, **kw):
        submit_planners.append(kw.get("planner") or "")
        return {"ok": True, "message": "queued", "job": {"task_ids": []}}

    monkeypatch.setattr("codepilot.webui_actions.submit_requirement_action", fake_submit)

    first = webui_mod.send_session_message_action(
        session["session"]["id"],
        "优化一下",
        category="requirement",
    )
    second = webui_mod.send_session_message_action(
        session["session"]["id"],
        "先做 Web UI",
        category="auto",
    )

    assert first["intent"] == "clarify"
    assert second["intent"] == "requirement"
    assert clarify_planners == ["claude", "claude"]
    assert submit_planners == ["claude"]


def test_webui_session_pending_clarification_error_uses_unified_error_exit(tmp_path, monkeypatch):
    _register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")

    monkeypatch.setattr(
        "codepilot.webui_actions.clarify_requirement",
        lambda *a, **kw: {
            "status": "needs_clarification",
            "questions": ["先做哪块?"],
            "qa_history": [],
        },
    )

    first = webui_mod.send_session_message_action(
        session["session"]["id"],
        "优化一下",
        category="requirement",
    )
    assert first["intent"] == "clarify"

    monkeypatch.setattr(
        "codepilot.webui_actions.continue_pending_clarification",
        lambda *a, **kw: {
            "status": "error",
            "error_kind": "runtime",
            "message": "clarifier backend exploded",
        },
    )

    with pytest.raises(RuntimeError, match="clarifier backend exploded"):
        webui_mod.send_session_message_action(
            session["session"]["id"],
            "先做 Web UI",
            category="auto",
        )
