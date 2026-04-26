"""Web UI clarification flow tests."""

from __future__ import annotations

import pytest

from codepilot.storage import database as db
from codepilot.webapp import server as webui_mod
from tests.chat_flow_testkit import register_project


def _q(text: str, *, qid: str = "q1", qtype: str = "text", options: list[tuple[str, str]] | None = None, allow_free_text: bool = False) -> dict:
    return {
        "id": qid,
        "type": qtype,
        "text": text,
        "options": [{"id": option_id, "label": label} for option_id, label in (options or [])],
        "allow_free_text": allow_free_text,
    }


def test_webui_submit_goal_returns_clarify(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(
        "codepilot.webapp.actions.clarify_requirement",
        lambda *a, **kw: {
            "status": "needs_clarification",
            "questions": [_q("要优化哪里?")],
            "qa_history": [],
        },
    )

    out = webui_mod.submit_goal_action("demo", "优化一下", category="requirement")
    assert out["intent"] == "clarify"
    assert out["questions"] == [_q("要优化哪里?")]
    assert out["original_title"] == "优化一下"


def test_webui_submit_goal_continues_with_history(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    def fake_clarify(title, *, project_info, qa_history=None, planner="codex", **kw):
        if qa_history and len(qa_history) >= 1:
            return {"status": "ready", "refined_title": title + " refined", "qa_history": qa_history}
        return {"status": "needs_clarification", "questions": [_q("?")], "qa_history": []}

    monkeypatch.setattr("codepilot.webapp.actions.clarify_requirement", fake_clarify)

    plan_calls = []

    def fake_submit(project, text, **kw):
        plan_calls.append(text)
        return {"ok": True, "message": "queued", "job": {"task_ids": []}}

    monkeypatch.setattr(
        "codepilot.webapp.actions.submit_requirement_action", fake_submit
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


def test_webui_submit_goal_builds_text_from_structured_answers_and_questions(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_dispatch(ctx):
        captured["text"] = ctx.text
        captured["questions"] = ctx.clarify_questions
        return {"ok": True, "intent": "noop", "message": "ok"}

    monkeypatch.setattr("codepilot.webapp.actions._dispatch_goal_by_intent", fake_dispatch)

    question = _q(
        "先覆盖哪个入口?",
        qid="entry",
        qtype="single",
        options=[("web", "Web UI"), ("cli", "CLI")],
        allow_free_text=True,
    )

    out = webui_mod.submit_goal_action(
        "demo",
        "",
        category="requirement",
        original_title="优化一下",
        clarify_questions=[question],
        clarify_answers=[{"question_id": "entry", "selected_option_ids": ["web"]}],
    )

    assert out["ok"] is True
    assert captured["text"] == "先覆盖哪个入口?：Web UI"
    assert captured["questions"] == [question]


def test_webui_submit_goal_rejects_empty_clarification_reply_without_answers(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="输入不能为空"):
        webui_mod.submit_goal_action(
            "demo",
            "",
            category="requirement",
            original_title="优化一下",
            clarify_questions=[_q("先做哪块?")],
            clarify_answers=[],
        )


def test_webui_submit_requirement_returns_clarify_before_job(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(
        "codepilot.webapp.actions.clarify_requirement",
        lambda *a, **kw: {
            "status": "needs_clarification",
            "questions": [_q("先覆盖哪个入口?")],
            "qa_history": [],
        },
    )

    out = webui_mod.submit_requirement_action(
        "demo",
        "做一个全自动编程工作流智能体",
        run_async=False,
    )

    assert out["intent"] == "clarify"
    assert out["questions"] == [_q("先覆盖哪个入口?")]
    assert webui_mod.list_ui_jobs("demo") == []


def test_webui_submit_requirement_rejects_empty_clarification_reply_without_answers(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="需求文本不能为空"):
        webui_mod.submit_requirement_action(
            "demo",
            "",
            original_title="优化一下",
            clarify_questions=[_q("先做哪块?")],
            clarify_answers=[],
            run_async=False,
        )


def test_webui_submit_requirement_continues_after_clarify_answer(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    def fake_clarify(title, *, qa_history=None, **kw):
        if qa_history:
            return {
                "status": "ready",
                "refined_title": title + " / 补充: " + qa_history[-1]["answer"],
                "qa_history": qa_history,
            }
        return {
            "status": "needs_clarification",
            "questions": [
                _q(
                    "先覆盖哪个入口?",
                    qid="entry",
                    qtype="single",
                    options=[("web", "Web UI"), ("cli", "CLI")],
                    allow_free_text=True,
                )
            ],
            "qa_history": [],
        }

    monkeypatch.setattr("codepilot.webapp.actions.clarify_requirement", fake_clarify)

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
        "",
        original_title="做一个全自动编程工作流智能体",
        qa_history=[],
        clarify_questions=[
            _q(
                "先覆盖哪个入口?",
                qid="entry",
                qtype="single",
                options=[("web", "Web UI"), ("cli", "CLI")],
                allow_free_text=True,
            )
        ],
        clarify_answers=[{"question_id": "entry", "selected_option_ids": ["web"]}],
        run_async=False,
    )

    assert out["ok"] is True
    assert out.get("intent") != "clarify"
    jobs = webui_mod.list_ui_jobs("demo")
    assert jobs and jobs[0]["task_ids"]
    task = db.get_task(jobs[0]["task_ids"][0])
    assert "Web UI" in task["title"]

