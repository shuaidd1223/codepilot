from __future__ import annotations

import json

from codepilot.feishu_bot import handle_command_text
from codepilot.storage import database as db
from tests.feishu_bot_testkit import _setup_project


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

def test_feishu_requirement_command_submits_goal_in_active_project(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    def fake_submit(project, text, **kwargs):
        calls.append({"project": project, "text": text, "kwargs": kwargs})
        return {"ok": True, "message": "需求已提交，后台任务 #9 已启动。", "job": {"id": 9, "status": "queued", "phase": "queued", "task_ids": []}}

    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    handle_command_text("use demo", chat_id="chat-req")
    reply = handle_command_text("需求 优化飞书任务面板", chat_id="chat-req")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert calls == [
        {
            "project": "demo",
            "text": "优化飞书任务面板",
            "kwargs": {"execute": True, "run_async": True, "task_source": "feishu:chat-req"},
        }
    ]
    assert "需求已提交" in payload
    assert "后台任务" in payload

def test_feishu_plain_text_in_project_context_submits_goal_action(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "codepilot.webapp.actions.submit_goal_action",
        lambda project, text, **kwargs: calls.append({"project": project, "text": text, "kwargs": kwargs}) or {
            "ok": True,
            "intent": "requirement",
            "message": "需求已提交，后台任务 #13 已启动。",
            "job": {"id": 13, "status": "queued", "phase": "queued", "task_ids": []},
        },
    )

    handle_command_text("use demo", chat_id="chat-plain")
    reply = handle_command_text("优化飞书任务面板", chat_id="chat-plain")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert calls == [{"project": "demo", "text": "优化飞书任务面板", "kwargs": {}}]
    assert "需求已提交" in payload
    assert "后台任务" in payload

def test_feishu_information_query_in_project_context_routes_to_question(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    monkeypatch.setattr(
        "codepilot.webapp.action_requirements._answer_project_question",
        lambda project_info, question, **kwargs: calls.append(
            {"project": project_info["name"], "question": question, "kwargs": kwargs}
        ) or "共有 2 个任务，已完成 1 个。",
    )

    handle_command_text("use demo", chat_id="chat-question")
    reply = handle_command_text("当前有多少任务，完成了多少", chat_id="chat-question")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert len(calls) == 1
    assert calls[0]["project"] == "demo"
    assert calls[0]["question"] == "当前有多少任务，完成了多少"
    assert "共有 2 个任务，已完成 1 个" in payload
    assert "需求已提交" not in payload

def test_feishu_auto_requirement_like_text_requires_explicit_prefix(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    handle_command_text("use demo", chat_id="chat-confirm")
    reply = handle_command_text("优化飞书任务面板", chat_id="chat-confirm")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert "不会直接执行" in payload
    assert "需求 <内容>" in payload
    assert "# <内容>" in payload

def test_feishu_explicit_requirement_prefix_still_submits_goal_action(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "codepilot.webapp.actions.submit_requirement_action",
        lambda project, text, **kwargs: calls.append({"project": project, "text": text, "kwargs": kwargs}) or {
            "ok": True,
            "intent": "requirement",
            "message": "需求已提交，后台任务 #13 已启动。",
            "job": {"id": 13, "status": "queued", "phase": "queued", "task_ids": []},
        },
    )

    handle_command_text("use demo", chat_id="chat-prefix")
    reply = handle_command_text("需求 优化飞书任务面板", chat_id="chat-prefix")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert calls == [{
        "project": "demo",
        "text": "优化飞书任务面板",
        "kwargs": {"execute": True, "run_async": True, "task_source": "feishu:chat-prefix"},
    }]
    assert "需求已提交" in payload

def test_feishu_requirement_clarification_uses_answer_command(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    def fake_submit(project, text, **kwargs):
        calls.append({"project": project, "text": text, "kwargs": kwargs})
        if len(calls) == 1:
            return {
                "ok": True,
                "intent": "clarify",
                "original_title": text,
                "questions": [{"id": "q1", "type": "text", "text": "先做哪个入口?", "options": []}],
                "qa_history": [],
            }
        return {"ok": True, "intent": "requirement", "message": "queued", "job": {"id": 10, "status": "queued", "phase": "queued", "task_ids": []}}

    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    handle_command_text("use demo", chat_id="chat-clarify")
    first = handle_command_text("需求 优化控制入口", chat_id="chat-clarify")
    second = handle_command_text("答 先做飞书", chat_id="chat-clarify")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    second_payload = json.dumps(second["card"], ensure_ascii=False)

    assert "需求澄清" in first_payload
    assert "答 <你的补充信息>" in first_payload
    assert calls[1]["text"] == ""
    assert calls[1]["kwargs"]["original_title"] == "优化控制入口"
    assert calls[1]["kwargs"]["clarify_answers"] == [
        {"question_id": "q1", "selected_option_ids": [], "free_text": "先做飞书"}
    ]
    assert "需求已提交" in second_payload

def test_feishu_choice_clarification_parses_numbered_answer(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    def fake_submit(project, text, **kwargs):
        calls.append({"project": project, "text": text, "kwargs": kwargs})
        if len(calls) == 1:
            return {
                "ok": True,
                "intent": "clarify",
                "original_title": text,
                "questions": [
                    {
                        "id": "scope",
                        "type": "multi",
                        "text": "先覆盖哪些入口?",
                        "options": [
                            {"id": "web", "label": "Web UI"},
                            {"id": "feishu", "label": "飞书"},
                            {"id": "cli", "label": "CLI"},
                        ],
                        "allow_free_text": False,
                    }
                ],
                "qa_history": [],
            }
        return {"ok": True, "intent": "requirement", "message": "queued", "job": {"id": 11, "status": "queued", "phase": "queued", "task_ids": []}}

    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    handle_command_text("use demo", chat_id="chat-choice")
    first = handle_command_text("需求 优化控制入口", chat_id="chat-choice")
    second = handle_command_text("答 1,2", chat_id="chat-choice")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    second_payload = json.dumps(second["card"], ensure_ascii=False)

    assert "[多选]" in first_payload
    assert "回复格式：答 1,2" in first_payload
    assert calls[1]["text"] == ""
    assert calls[1]["kwargs"]["clarify_answers"] == [
        {"question_id": "scope", "selected_option_ids": ["web", "feishu"], "free_text": ""}
    ]
    assert "需求已提交" in second_payload

def test_feishu_requirement_command_pushes_planning_progress_cards(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    sent_cards = []

    def fake_send(card, *, project_name="", chat_ids=None):
        sent_cards.append({"card": card, "project_name": project_name, "chat_ids": chat_ids})
        return True

    def fake_submit(project, text, **kwargs):
        from codepilot.core import progress_bus

        progress_bus.emit(stage="planner", event_type="phase_start", message="开始规划")
        progress_bus.emit(stage="planner", event_type="phase_end", message="规划完成")
        return {"ok": True, "message": "queued", "job": {"id": 12, "status": "queued", "phase": "queued", "task_ids": []}}

    monkeypatch.setattr("codepilot.feishu_bot._send_bot_card", fake_send)
    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    handle_command_text("use demo", chat_id="chat-progress")
    reply = handle_command_text("需求 优化飞书进度反馈", chat_id="chat-progress")
    payload = json.dumps([item["card"] for item in sent_cards], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "需求规划开始" in payload
    assert "规划完成" in payload
    assert all(item["chat_ids"] == ["chat-progress"] for item in sent_cards)
