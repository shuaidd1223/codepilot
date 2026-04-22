"""Web UI session clarification flow tests."""

from __future__ import annotations

import pytest

from codepilot import db
from codepilot import webui as webui_mod
from tests.chat_flow_testkit import register_project


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
    register_project(tmp_path, monkeypatch)
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
