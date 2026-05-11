from __future__ import annotations

from pathlib import Path

from codepilot.storage import database as db
from tests.chat_flow_testkit import register_project


def test_webui_session_message_uses_opencode_adapter(tmp_path: Path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = db.create_session("demo", title="chat")
    calls = []

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
            "intent": "opencode",
            "message": "OpenCode 已处理。",
            "opencode_session_id": "ses_web",
            "tool_calls": [{"name": "codepilot.health"}],
        }

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)

    from codepilot.webapp.action_sessions import send_session_message_action

    result = send_session_message_action(session["id"], "帮我看一下任务状态", category="requirement")

    assert result["intent"] == "opencode"
    assert result["message"] == "OpenCode 已处理。"
    assert calls == [
        {
            "project": "demo",
            "text": "帮我看一下任务状态",
            "source": "web",
            "external_session_id": str(session["id"]),
        }
    ]
    messages = db.list_session_messages(session["id"])
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["intent"] == "opencode"


def test_webui_session_message_records_opencode_error(tmp_path: Path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = db.create_session("demo", title="chat")

    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda *_args, **_kwargs: {"ok": False, "message": "OpenCode 执行失败：缺少 provider"},
    )

    from codepilot.webapp.action_sessions import send_session_message_action

    result = send_session_message_action(session["id"], "你好")

    assert result["ok"] is False
    assert result["intent"] == "error"
    assert "OpenCode 执行失败" in result["message"]
    assert db.list_session_messages(session["id"])[1]["intent"] == "error"
