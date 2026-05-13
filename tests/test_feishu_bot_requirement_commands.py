from __future__ import annotations

import json

from codepilot.feishu_bot import handle_command_text
from codepilot.storage import database as db
from tests.feishu_bot_testkit import _setup_project


def _stub_opencode(monkeypatch, calls: list[dict], *, message: str = "OpenCode 已处理。"):
    def fake_run(project, text, *, source, external_session_id):
        calls.append({"project": project, "text": text, "source": source, "external_session_id": external_session_id})
        return {"ok": True, "message": message, "opencode_session_id": f"ses-{len(calls)}"}

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)


def test_feishu_use_project_sets_chat_context(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="查看当前项目上下文",
        content="验证飞书会话项目切换",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    reply = handle_command_text("use demo", chat_id="chat-1")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    assert "已切换项目" in payload

    tasks_reply = handle_command_text("tasks", chat_id="chat-1")
    tasks_payload = json.dumps(tasks_reply["card"], ensure_ascii=False)
    assert "CodePilot 任务面板" in tasks_payload
    assert "查看当前项目上下文" in tasks_payload

def test_feishu_requirement_command_enters_opencode_in_active_project(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 已接收需求。")

    handle_command_text("use demo", chat_id="chat-req")
    reply = handle_command_text("需求 优化飞书任务面板", chat_id="chat-req")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    session_id = str(db.list_sessions(project="demo")[0]["id"])

    assert calls == [
        {
            "project": "demo",
            "text": "优化飞书任务面板",
            "source": "feishu",
            "external_session_id": session_id,
        }
    ]
    assert "OpenCode 会话" in payload
    assert "OpenCode 已接收需求" in payload

def test_feishu_plain_text_in_project_context_uses_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 按自由文本处理。")

    handle_command_text("use demo", chat_id="chat-plain")
    reply = handle_command_text("优化飞书任务面板", chat_id="chat-plain")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    session_id = str(db.list_sessions(project="demo")[0]["id"])

    assert calls == [
        {"project": "demo", "text": "优化飞书任务面板", "source": "feishu", "external_session_id": session_id}
    ]
    assert "OpenCode 会话" in payload
    assert "OpenCode 按自由文本处理" in payload

def test_feishu_information_query_in_project_context_uses_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="共有 2 个任务，已完成 1 个。")

    handle_command_text("use demo", chat_id="chat-question")
    reply = handle_command_text("当前有多少任务，完成了多少", chat_id="chat-question")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    session_id = str(db.list_sessions(project="demo")[0]["id"])

    assert calls == [
        {"project": "demo", "text": "当前有多少任务，完成了多少", "source": "feishu", "external_session_id": session_id}
    ]
    assert "共有 2 个任务，已完成 1 个" in payload
    assert "需求已提交" not in payload

def test_feishu_auto_requirement_like_text_goes_to_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 已接管自由文本。")
    handle_command_text("use demo", chat_id="chat-confirm")
    reply = handle_command_text("优化飞书任务面板", chat_id="chat-confirm")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert calls[0]["text"] == "优化飞书任务面板"
    assert "OpenCode 已接管自由文本" in payload
    assert "不会直接执行" not in payload

def test_feishu_explicit_requirement_prefix_uses_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 已收到显式需求。")

    handle_command_text("use demo", chat_id="chat-prefix")
    reply = handle_command_text("需求 优化飞书任务面板", chat_id="chat-prefix")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    session_id = str(db.list_sessions(project="demo")[0]["id"])

    assert calls == [{
        "project": "demo",
        "text": "优化飞书任务面板",
        "source": "feishu",
        "external_session_id": session_id,
    }]
    assert "OpenCode 已收到显式需求" in payload

def test_feishu_answer_command_goes_to_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 会话继续。")

    handle_command_text("use demo", chat_id="chat-clarify")
    first = handle_command_text("需求 优化控制入口", chat_id="chat-clarify")
    second = handle_command_text("答 先做飞书", chat_id="chat-clarify")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    second_payload = json.dumps(second["card"], ensure_ascii=False)

    assert calls[0]["text"] == "优化控制入口"
    assert calls[1]["text"] == "先做飞书"
    assert "需求澄清" not in first_payload
    assert "OpenCode 会话继续" in second_payload

def test_feishu_numbered_answer_text_goes_to_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 已接收编号文本。")

    handle_command_text("use demo", chat_id="chat-choice")
    first = handle_command_text("需求 优化控制入口", chat_id="chat-choice")
    second = handle_command_text("答 1,2", chat_id="chat-choice")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    second_payload = json.dumps(second["card"], ensure_ascii=False)

    assert calls[0]["text"] == "优化控制入口"
    assert calls[1]["text"] == "1,2"
    assert "[多选]" not in first_payload
    assert "OpenCode 已接收编号文本" in second_payload

def test_feishu_requirement_command_does_not_emit_old_planning_progress_cards(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    sent_cards = []

    def fake_send(card, *, project_name="", chat_ids=None):
        sent_cards.append({"card": card, "project_name": project_name, "chat_ids": chat_ids})
        return True

    monkeypatch.setattr("codepilot.feishu_bot.card_builders._send_bot_card", fake_send)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 会话已启动。")

    handle_command_text("use demo", chat_id="chat-progress")
    reply = handle_command_text("需求 优化飞书进度反馈", chat_id="chat-progress")
    payload = json.dumps([item["card"] for item in sent_cards], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert calls[0]["text"] == "优化飞书进度反馈"
    assert "需求规划开始" not in payload
    assert "规划完成" not in payload
