"""Web UI session OpenCode routing tests."""

from __future__ import annotations

from codepilot.storage import database as db
from codepilot.webapp import server as webui_mod
from tests.chat_flow_testkit import register_project


def test_webui_session_message_uses_opencode_adapter(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="chat")
    calls: list[dict[str, str]] = []

    def fake_run(project, text, *, source, external_session_id):
        calls.append(
            {
                "project": project,
                "text": text,
                "source": source,
                "external_session_id": external_session_id,
            }
        )
        return {
            "ok": True,
            "message": "OpenCode 已处理。",
            "opencode_session_id": "ses-web",
            "tool_calls": [{"name": "codepilot_list_tasks"}],
        }

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)

    out = webui_mod.send_session_message_action(session["session"]["id"], "优化一下任务列表", category="requirement")
    detail = webui_mod.get_session_action(session["session"]["id"])

    assert out["intent"] == "opencode"
    assert out["opencode_session_id"] == "ses-web"
    assert calls == [
        {
            "project": "demo",
            "text": "优化一下任务列表",
            "source": "web",
            "external_session_id": str(session["session"]["id"]),
        }
    ]
    assert [(msg["role"], msg["intent"]) for msg in detail["messages"]] == [
        ("user", None),
        ("assistant", "opencode"),
    ]
    assert detail["messages"][1]["metadata"]["tool_calls"] == [{"name": "codepilot_list_tasks"}]


def test_webui_session_keeps_existing_legacy_clarify_message_as_history_only(tmp_path, monkeypatch):
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
    calls: list[str] = []
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda project, text, *, source, external_session_id: calls.append(text)
        or {"ok": True, "message": "已继续交给 OpenCode。", "opencode_session_id": "ses-legacy"},
    )

    out = webui_mod.send_session_message_action(session_id, "先做 Web UI", category="auto")

    assert out["intent"] == "opencode"
    assert calls == ["先做 Web UI"]


def test_webui_session_error_from_opencode_is_saved_as_error(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="")
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda *a, **kw: {"ok": False, "message": "OpenCode 执行失败：stderr 摘要"},
    )

    out = webui_mod.send_session_message_action(session["session"]["id"], "看一下状态", category="question")
    detail = webui_mod.get_session_action(session["session"]["id"])

    assert out["ok"] is False
    assert out["intent"] == "error"
    assert detail["messages"][-1]["intent"] == "error"
    assert "stderr 摘要" in detail["messages"][-1]["content"]


def test_webui_session_rejects_empty_text_before_opencode(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = webui_mod.create_session_action("demo", title="")
    calls: list[object] = []
    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", lambda *a, **kw: calls.append(True))

    try:
        webui_mod.send_session_message_action(session["session"]["id"], "", category="auto", clarify_answers=[])
    except RuntimeError as exc:
        assert "输入不能为空" in str(exc)
    else:
        raise AssertionError("empty message should fail")
    assert calls == []
