"""Web UI session clarification flow tests."""

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


def test_webui_session_clarification_follows_project_planner(tmp_path, monkeypatch):
    project_path = register_project(tmp_path, monkeypatch)
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
        return {"status": "needs_clarification", "questions": [_q("先做哪块?")], "qa_history": []}

    monkeypatch.setattr("codepilot.webapp.actions.clarify_requirement", fake_clarify)

    def fake_submit(project, text, **kw):
        submit_planners.append(kw.get("planner") or "")
        return {"ok": True, "message": "queued", "job": {"task_ids": []}}

    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

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
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")

    monkeypatch.setattr(
        "codepilot.webapp.actions.clarify_requirement",
        lambda *a, **kw: {
            "status": "needs_clarification",
            "questions": [_q("先做哪块?")],
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
        "codepilot.webapp.actions.continue_pending_clarification",
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


def test_webui_session_accepts_structured_choice_answers(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")

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
                    "先做哪块?",
                    qid="scope",
                    qtype="single",
                    options=[("web", "Web UI"), ("cli", "CLI")],
                    allow_free_text=True,
                )
            ],
            "qa_history": [],
        }

    monkeypatch.setattr("codepilot.webapp.actions.clarify_requirement", fake_clarify)

    planned: list[str] = []

    def fake_submit(project, text, **kw):
        planned.append(text)
        return {"ok": True, "message": "queued", "job": {"task_ids": []}}

    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    first = webui_mod.send_session_message_action(
        session["session"]["id"],
        "优化一下",
        category="requirement",
    )
    second = webui_mod.send_session_message_action(
        session["session"]["id"],
        "",
        category="auto",
        clarify_answers=[{"question_id": "scope", "selected_option_ids": ["web"]}],
    )

    assert first["intent"] == "clarify"
    assert second["intent"] == "requirement"
    assert planned and "Web UI" in planned[0]


def test_webui_session_rejects_structured_answer_without_pending_question(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="")
    captured: dict[str, object] = {}

    def fake_dispatch(ctx, existing_messages):
        captured["text"] = ctx.text
        captured["existing_messages"] = existing_messages
        return {"ok": True, "intent": "info", "message": "ok"}

    monkeypatch.setattr("codepilot.webapp.actions._dispatch_session_message", fake_dispatch)

    out = webui_mod.send_session_message_action(
        session["session"]["id"],
        "",
        category="auto",
        clarify_answers=[{"question_id": "scope", "selected_option_ids": ["web"]}],
    )

    assert out["intent"] == "info"
    assert "没有正在等待回答" in out["message"]
    assert captured == {}
    assert db.get_session(session["session"]["id"])["title"] == "新会话"
    detail = webui_mod.get_session_action(session["session"]["id"])
    assert detail["messages"] == []


def test_webui_session_rejects_text_plus_structured_answer_without_pending_question(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")

    out = webui_mod.send_session_message_action(
        session["session"]["id"],
        "先做 Web UI",
        category="auto",
        clarify_answers=[{"question_id": "scope", "selected_option_ids": ["web"]}],
    )
    detail = webui_mod.get_session_action(session["session"]["id"])

    assert out["intent"] == "info"
    assert "没有正在等待回答" in out["message"]
    assert detail["messages"] == []


def test_webui_session_clear_without_pending_is_server_side_noop(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")

    dispatched = []
    monkeypatch.setattr(
        "codepilot.webapp.actions._dispatch_session_message",
        lambda *a, **kw: dispatched.append(True) or {"ok": True, "intent": "bad", "message": "bad"},
    )

    out = webui_mod.send_session_message_action(
        session["session"]["id"],
        "/clear",
        category="auto",
    )
    detail = webui_mod.get_session_action(session["session"]["id"])

    assert out["intent"] == "info"
    assert "没有正在等待澄清" in out["message"]
    assert dispatched == []
    assert [msg["intent"] for msg in detail["messages"]] == [None, "info"]


def test_webui_session_rejects_stale_structured_answer_for_pending_clarification(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")
    session_id = session["session"]["id"]
    question = _q(
        "先做哪块?",
        qid="scope",
        qtype="single",
        options=[("web", "Web UI")],
        allow_free_text=False,
    )
    db.create_session_message(session_id, "user", "优化一下")
    db.create_session_message(
        session_id,
        "assistant",
        "1. 先做哪块?",
        intent="clarify",
        metadata={"questions": [question]},
    )

    out = webui_mod.send_session_message_action(
        session_id,
        "",
        category="auto",
        clarify_answers=[{"question_id": "old_scope", "selected_option_ids": ["web"]}],
    )
    detail = webui_mod.get_session_action(session_id)

    assert out["intent"] == "info"
    assert "过期" in out["message"]
    assert [(msg["role"], msg["intent"]) for msg in detail["messages"]] == [
        ("user", None),
        ("assistant", "clarify"),
    ]


def test_webui_session_reconstructs_legacy_pending_clarification_without_metadata(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")
    session_id = session["session"]["id"]
    db.create_session_message(session_id, "user", "优化一下")
    db.create_session_message(
        session_id,
        "assistant",
        "为了更好地规划，请先确认以下几个点：\n1. 先做哪块?",
        intent="clarify",
    )

    captured: dict[str, object] = {}

    def fake_pending(ctx, pending):
        captured["text"] = ctx.text
        captured["questions"] = pending.get("last_questions")
        return {"ok": True, "intent": "info", "message": "ok"}

    monkeypatch.setattr("codepilot.webapp.actions._dispatch_session_pending_clarification", fake_pending)

    out = webui_mod.send_session_message_action(
        session_id,
        "先做 Web UI",
        category="auto",
    )

    assert out["ok"] is True
    assert captured["text"] == "先做 Web UI"
    assert captured["questions"] == [{
        "id": "legacy_q1",
        "type": "text",
        "text": "先做哪块?",
        "options": [],
        "allow_free_text": False,
    }]


def test_webui_session_question_uses_previous_turn_history(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")
    session_id = session["session"]["id"]
    db.create_session_message(session_id, "user", "第一个需求是优化任务列表")
    db.create_session_message(session_id, "assistant", "已创建任务 #1", intent="requirement", task_ids=[1])

    captured: dict[str, object] = {}

    def fake_answer(project_info, question, **kwargs):
        captured["question"] = question
        captured["history"] = kwargs.get("history")
        return "会结合上一个需求继续说明。"

    monkeypatch.setattr("codepilot.webapp.action_sessions._answer_project_question", fake_answer)

    out = webui_mod.send_session_message_action(
        session_id,
        "刚才那个需求现在做到哪了？",
        category="question",
    )

    assert out["intent"] == "question"
    assert captured["question"] == "刚才那个需求现在做到哪了？"
    assert captured["history"] == [
        {"user": "第一个需求是优化任务列表", "assistant": "已创建任务 #1"}
    ]


def test_webui_session_requirement_planning_includes_previous_turn_context(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")
    session_id = session["session"]["id"]
    db.create_session_message(session_id, "user", "第一个需求：优化任务列表")
    db.create_session_message(session_id, "assistant", "需求已提交，后台任务 #9。", intent="requirement", task_ids=[9])

    assessed: dict[str, str] = {}
    submitted: dict[str, str] = {}

    def fake_assess(text, **kwargs):
        assessed["text"] = text
        return {"status": "ready", "refined_title": "在上一个需求基础上增加筛选"}

    def fake_submit(project, text, **kwargs):
        submitted["text"] = text
        return {"ok": True, "message": "queued", "job": {"task_ids": [2]}}

    monkeypatch.setattr("codepilot.webapp.action_sessions._assess_requirement", fake_assess)
    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    out = webui_mod.send_session_message_action(
        session_id,
        "# 第二个需求：在刚才基础上继续加筛选",
        category="auto",
    )

    assert out["intent"] == "requirement"
    assert "## 会话上下文" in assessed["text"]
    assert "第一个需求：优化任务列表" in assessed["text"]
    assert "tasks=[9]" in assessed["text"]
    assert submitted["text"].startswith("在上一个需求基础上增加筛选")
    assert "## 会话上下文" in submitted["text"]
    assert "第一个需求：优化任务列表" in submitted["text"]


def test_webui_session_clarification_preserves_session_context_until_submit(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")
    session_id = session["session"]["id"]
    db.create_session_message(session_id, "user", "第一个需求：优化任务列表")
    db.create_session_message(session_id, "assistant", "需求已提交。", intent="requirement", task_ids=[7])

    clarify_calls: list[str] = []
    submitted: dict[str, str] = {}

    def fake_clarify(title, *, qa_history=None, **kwargs):
        clarify_calls.append(title)
        if qa_history:
            return {"status": "ready", "refined_title": "继续补充空状态", "qa_history": qa_history}
        return {"status": "needs_clarification", "questions": [_q("先补哪块?")], "qa_history": []}

    def fake_submit(project, text, **kwargs):
        submitted["text"] = text
        return {"ok": True, "message": "queued", "job": {"task_ids": [3]}}

    monkeypatch.setattr("codepilot.webapp.actions.clarify_requirement", fake_clarify)
    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    first = webui_mod.send_session_message_action(
        session_id,
        "# 继续完善刚才那个",
        category="auto",
    )
    second = webui_mod.send_session_message_action(
        session_id,
        "先补空状态",
        category="auto",
    )

    assert first["intent"] == "clarify"
    assert second["intent"] == "requirement"
    assert "## 会话上下文" in clarify_calls[0]
    assert "第一个需求：优化任务列表" in submitted["text"]
    assert "## 会话上下文" in submitted["text"]


def test_webui_session_reconstructs_legacy_question_prefix_variants(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")
    session_id = session["session"]["id"]
    db.create_session_message(session_id, "user", "优化一下")
    db.create_session_message(
        session_id,
        "assistant",
        "为了更好地规划，请先确认以下几个点：\n  问题1：先做哪块?\n二、目标是什么?",
        intent="clarify",
    )

    captured: dict[str, object] = {}

    def fake_pending(ctx, pending):
        captured["questions"] = pending.get("last_questions")
        return {"ok": True, "intent": "info", "message": "ok"}

    monkeypatch.setattr("codepilot.webapp.actions._dispatch_session_pending_clarification", fake_pending)

    out = webui_mod.send_session_message_action(session_id, "先做 Web UI", category="auto")

    assert out["ok"] is True
    assert captured["questions"] == [
        {
            "id": "legacy_q1",
            "type": "text",
            "text": "先做哪块?",
            "options": [],
            "allow_free_text": False,
        },
        {
            "id": "legacy_q2",
            "type": "text",
            "text": "目标是什么?",
            "options": [],
            "allow_free_text": False,
        },
    ]


def test_webui_session_legacy_parser_ignores_indented_option_lines(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")
    session_id = session["session"]["id"]
    db.create_session_message(session_id, "user", "优化一下")
    db.create_session_message(
        session_id,
        "assistant",
        "1. 先覆盖哪个入口?\n   1. Web UI\n   2. CLI",
        intent="clarify",
    )

    captured: dict[str, object] = {}

    def fake_pending(ctx, pending):
        captured["questions"] = pending.get("last_questions")
        return {"ok": True, "intent": "info", "message": "ok"}

    monkeypatch.setattr("codepilot.webapp.actions._dispatch_session_pending_clarification", fake_pending)

    out = webui_mod.send_session_message_action(session_id, "Web UI", category="auto")

    assert out["ok"] is True
    assert captured["questions"] == [{
        "id": "legacy_q1",
        "type": "text",
        "text": "先覆盖哪个入口?",
        "options": [],
        "allow_free_text": False,
    }]


def test_webui_session_can_cancel_pending_clarification(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")

    monkeypatch.setattr(
        "codepilot.webapp.actions.clarify_requirement",
        lambda *a, **kw: {
            "status": "needs_clarification",
            "questions": [_q("先做哪块?")],
            "qa_history": [],
        },
    )

    first = webui_mod.send_session_message_action(
        session["session"]["id"],
        "优化一下",
        category="requirement",
    )
    cancelled = webui_mod.send_session_message_action(
        session["session"]["id"],
        "/clear",
        category="auto",
    )
    detail = webui_mod.get_session_action(session["session"]["id"])

    assert first["intent"] == "clarify"
    assert cancelled["intent"] == "info"
    assert "已取消当前这次需求规划" in cancelled["message"]
    assert detail["messages"][-1]["intent"] == "info"

