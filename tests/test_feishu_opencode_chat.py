from __future__ import annotations

import json

from codepilot.feishu_bot import handle_command_text
from codepilot.storage import database as db
from tests.feishu_bot_testkit import _setup_project


def test_feishu_plain_text_in_project_context_uses_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    def fake_run(project, text, *, source, external_session_id):
        calls.append({"project": project, "text": text, "source": source, "external_session_id": external_session_id})
        return {"ok": True, "message": "OpenCode 已经处理这条消息。", "opencode_session_id": "ses_f"}

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)

    handle_command_text("use demo", chat_id="chat-opencode")
    reply = handle_command_text("优化飞书任务面板", chat_id="chat-opencode")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    sessions = db.list_sessions(project="demo")

    assert len(sessions) == 1
    assert calls == [{
        "project": "demo",
        "text": "优化飞书任务面板",
        "source": "feishu",
        "external_session_id": str(sessions[0]["id"]),
    }]
    messages = db.list_session_messages(int(sessions[0]["id"]))
    assert [(message["role"], message["intent"]) for message in messages] == [("user", None), ("assistant", "opencode")]
    assert json.loads(messages[1]["metadata"])["opencode_session_id"] == "ses_f"
    assert "OpenCode 会话" in payload
    assert "OpenCode 已经处理这条消息" in payload


def test_feishu_plain_text_without_project_prompts_for_project(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    for name in ("demo-a", "demo-b"):
        path = tmp_path / name
        path.mkdir()
        db.register_project(name, str(path))

    calls = []
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True, "message": "unexpected"},
    )

    reply = handle_command_text("帮我看一下状态", chat_id="chat-no-project")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert calls == []
    assert "先确认你要在哪个项目里继续" in payload
    assert "在项目 demo-a 中继续" in payload


def test_feishu_project_choice_continues_pending_text_in_opencode(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    for name in ("demo-a", "demo-b"):
        path = tmp_path / name
        path.mkdir()
        db.register_project(name, str(path))

    calls = []
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda project, text, *, source, external_session_id: calls.append(
            {"project": project, "text": text, "source": source, "external_session_id": external_session_id}
        )
        or {"ok": True, "message": "OpenCode 已继续处理。", "opencode_session_id": "ses-choice"},
    )

    handle_command_text("帮我看一下状态", chat_id="chat-project-choice")
    reply = handle_command_text("1", chat_id="chat-project-choice")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    sessions = db.list_sessions(project="demo-a")

    assert calls == [
        {
            "project": "demo-a",
            "text": "帮我看一下状态",
            "source": "feishu",
            "external_session_id": str(sessions[0]["id"]),
        }
    ]
    assert "OpenCode 已继续处理" in payload


def test_feishu_requirement_keyword_also_uses_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda project, text, *, source, external_session_id: calls.append(
            {"project": project, "text": text, "source": source, "external_session_id": external_session_id}
        )
        or {"ok": True, "message": "已进入 OpenCode 会话。", "opencode_session_id": "ses_req"},
    )

    handle_command_text("use demo", chat_id="chat-req-opencode")
    reply = handle_command_text("需求 优化控制入口", chat_id="chat-req-opencode")

    assert calls[0]["text"] == "优化控制入口"
    assert "OpenCode 会话" in json.dumps(reply["card"], ensure_ascii=False)


def test_feishu_session_reply_routes_to_requested_session(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    first = db.create_session("demo", "一号会话")
    second = db.create_session("demo", "二号会话")
    calls = []

    def fake_run(project, text, *, source, external_session_id):
        calls.append({"project": project, "text": text, "source": source, "external_session_id": external_session_id})
        return {"ok": True, "message": "已继续指定会话。", "opencode_session_id": "ses-specific"}

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)

    reply = handle_command_text(f"session reply {first['id']} 继续处理一号", chat_id="chat-session-reply")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert calls == [{
        "project": "demo",
        "text": "继续处理一号",
        "source": "feishu",
        "external_session_id": str(first["id"]),
    }]
    assert "已继续指定会话" in payload
    assert [message["role"] for message in db.list_session_messages(int(first["id"]))] == ["user", "assistant"]
    assert db.list_session_messages(int(second["id"])) == []
