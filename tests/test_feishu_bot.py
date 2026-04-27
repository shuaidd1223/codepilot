from __future__ import annotations

import json
import os
import sys

from codepilot.commands import feishu as feishu_cmd
from codepilot.feishu_bot import build_task_event_card, handle_command_text, notify_feishu_task_event
from codepilot.storage import database as db


def _setup_project(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def test_feishu_help_returns_interactive_card(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    reply = handle_command_text("help")

    assert reply["type"] == "interactive"
    assert "CodePilot 飞书命令" in reply["card"]["header"]["title"]["content"]


def test_feishu_tasks_card_lists_project_tasks(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="修复飞书命令面板",
        content="补全卡片内容",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )

    reply = handle_command_text("tasks demo")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "CodePilot 任务面板" in payload
    assert "修复飞书命令面板" in payload
    assert "stop 1" in payload
    assert "delete 1" in payload
    assert "下一步命令" in payload
    assert "最近任务" in payload


def test_feishu_status_command_works_without_webui_server(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    server_module = sys.modules.pop("codepilot.webapp.server", None)
    try:
        reply = handle_command_text("status demo")
    finally:
        if server_module is not None:
            sys.modules["codepilot.webapp.server"] = server_module
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "任务执行服务状态" in payload
    assert "demo" in payload
    assert "未运行" in payload


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
            "kwargs": {"execute": True, "run_async": False, "task_source": "feishu:chat-req"},
        }
    ]
    assert "需求已提交" in payload
    assert "后台任务" in payload


def test_feishu_plain_text_in_project_context_does_not_submit_requirement(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", lambda *args, **kwargs: calls.append(args) or {})

    handle_command_text("use demo", chat_id="chat-plain")
    reply = handle_command_text("优化飞书任务面板", chat_id="chat-plain")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert calls == []
    assert "未识别命令" in payload
    assert "需求 优化飞书任务面板" not in payload


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


def test_feishu_global_status_card_lists_projects_and_services(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    other_path = tmp_path / "other"
    other_path.mkdir()
    db.register_project("other", str(other_path))
    db.upsert_service_state(
        "daemon",
        "demo",
        pid=os.getpid(),
        status="running",
        log_path=str(tmp_path / "daemon.log"),
        meta={"started_at": "2026-04-28T00:00:00", "project": "demo"},
    )
    db.upsert_service_state(
        "feishu",
        "_global",
        pid=os.getpid(),
        status="running",
        log_path=str(tmp_path / "feishu.log"),
        meta={"started_at": "2026-04-28T00:00:01"},
    )

    reply = handle_command_text("global")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "CodePilot 全局状态" in payload
    assert "注册项目" in payload
    assert "demo" in payload
    assert "other" in payload
    assert "飞书长连接" in payload
    assert "任务轮询" in payload
    assert any(elem.get("tag") == "column_set" for elem in reply["card"]["elements"] if isinstance(elem, dict))


def test_feishu_event_card_uses_structured_blocks():
    card = build_task_event_card(
        project_name="demo",
        task_id=12,
        task_title="优化飞书卡片",
        event="phase_end",
        phase="builder",
        status="in_progress",
        message="builder 阶段完成，准备 review",
    )

    tags = [elem.get("tag") for elem in card["elements"] if isinstance(elem, dict)]

    assert "CodePilot · Build 完成" in card["header"]["title"]["content"]
    assert "note" in tags
    assert "column_set" in tags
    assert "下一步命令" in json.dumps(card, ensure_ascii=False)


def test_feishu_task_notifications_dedupe_same_event(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    def fake_send(card, *, project_name="", chat_ids=None):
        calls.append((card, project_name, chat_ids))
        return True

    monkeypatch.setattr("codepilot.feishu_bot._send_bot_card", fake_send)

    kwargs = {
        "project_name": "demo",
        "project_path": str(tmp_path / "demo"),
        "task_id": 7,
        "task_title": "重复预检通知",
        "event": "preflight_skip",
        "phase": "preflight",
        "level": "warning",
        "message": "主工作区已有未提交改动",
        "status": "backlog",
    }

    assert notify_feishu_task_event(**kwargs) is True
    assert notify_feishu_task_event(**kwargs) is False
    assert len(calls) == 1


def test_feishu_delete_task_command_returns_deleted_card(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="删除飞书测试任务",
        content="验证删除命令",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    reply = handle_command_text(f"delete {task['id']}")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "任务已删除" in payload
    assert f"#{task['id']}" in payload
    assert db.get_task(task["id"]) is None


def test_feishu_unknown_command_returns_help_card(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    reply = handle_command_text("wat")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "未识别命令" in payload


def test_ensure_feishu_service_running_if_enabled_returns_disabled_when_config_off(monkeypatch):
    monkeypatch.setattr(
        feishu_cmd,
        "load_feishu_bot_config",
        lambda: type("Cfg", (), {"enabled": False})(),
    )

    result = feishu_cmd.ensure_service_running_if_enabled()

    assert result == {"enabled": False, "running": False, "started": False}


def test_ensure_feishu_service_running_if_enabled_starts_detached_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(
        feishu_cmd,
        "load_feishu_bot_config",
        lambda: type("Cfg", (), {"enabled": True})(),
    )
    monkeypatch.setattr(feishu_cmd, "_check_runtime_ready", lambda: None)
    monkeypatch.setattr(feishu_cmd, "_service_status", lambda: {"running": False})
    monkeypatch.setattr(feishu_cmd, "_clear_state", lambda: None)
    monkeypatch.setattr(feishu_cmd, "LOG_FILE", tmp_path / "feishu.log")
    monkeypatch.setattr(feishu_cmd.time, "sleep", lambda _seconds: None)

    class _Proc:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(feishu_cmd, "_spawn_detached", lambda: _Proc())

    result = feishu_cmd.ensure_service_running_if_enabled()

    assert result["enabled"] is True
    assert result["running"] is True
    assert result["started"] is True
    assert result["pid"] == 4321
