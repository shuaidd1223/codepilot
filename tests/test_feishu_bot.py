from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from codepilot.commands import feishu as feishu_cmd
from codepilot.feishu_bot import build_task_event_card, handle_command_text, handle_event_payload, notify_feishu_task_event
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
    assert "任务列表" in payload


def test_feishu_tasks_card_supports_status_filter(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    backlog = db.create_task(
        project="demo",
        title="待执行任务",
        content="只应出现在 backlog 视图",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    done = db.create_task(
        project="demo",
        title="已完成任务",
        content="只应出现在 done 视图",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )
    db.update_task(done["id"], status="done", completed_at=datetime.now().isoformat())
    reply = handle_command_text("tasks demo status=done")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "当前筛选" in payload
    assert "已完成" in payload
    assert "已完成任务" in payload
    assert "待执行任务" not in payload
    assert "tasks demo status=all" in payload
    assert f"detail {done['id']}" in payload
    assert f"detail {backlog['id']}" not in payload


def test_feishu_tasks_card_supports_pagination(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    for idx in range(1, 10):
        db.create_task(
            project="demo",
            title=f"分页任务 {idx:02d}",
            content="验证分页",
            agent="dual",
            priority="P2",
            project_path=str(project_path),
        )

    reply = handle_command_text("tasks demo status=backlog page=2")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "当前页" in payload
    assert "2/2" in payload
    assert "分页任务 09" in payload
    assert "分页任务 01" not in payload
    assert "上一页" in payload
    assert "tasks demo status=backlog" in payload
    assert "下一页" not in payload


def test_feishu_tasks_card_empty_filter_result(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="普通任务",
        content="没有归档任务",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    reply = handle_command_text("tasks demo archived")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "当前筛选下没有可展示的任务" in payload
    assert "tasks demo status=all" in payload


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


def test_feishu_service_commands_dispatch_by_service_type(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    def fake_project_service_action(project, service, action):
        calls.append({"project": project, "service": service, "action": action})
        return {"status": {"running": action == "start", "pid": 123, "started_at": "", "log": ""}, "message": "ok"}

    monkeypatch.setattr("codepilot.feishu_bot.project_service_action", fake_project_service_action)

    daemon_reply = handle_command_text("daemon stop demo")
    inspect_reply = handle_command_text("inspect start demo")
    daemon_payload = json.dumps(daemon_reply["card"], ensure_ascii=False)
    inspect_payload = json.dumps(inspect_reply["card"], ensure_ascii=False)

    assert calls == [
        {"project": "demo", "service": "tasks", "action": "stop"},
        {"project": "demo", "service": "inspect", "action": "start"},
    ]
    assert "任务轮询停止请求" in daemon_payload
    assert "巡检服务已启动" in inspect_payload


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
        "kwargs": {"execute": True, "run_async": False, "task_source": "feishu:chat-prefix"},
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
    assert any(
        elem.get("text", {}).get("tag") == "lark_md"
        for elem in card["elements"]
        if isinstance(elem, dict) and elem.get("tag") == "div"
    )
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


def test_feishu_delete_task_command_requires_confirm_token(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="删除飞书测试任务",
        content="验证删除命令",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    first = handle_command_text(f"delete {task['id']}", chat_id="chat-delete")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    pending = db.get_service_state("feishu_confirm", "chat-delete")
    token = pending["meta"]["token"]

    assert first["type"] == "interactive"
    assert "敏感操作待确认" in first_payload
    assert f"confirm {token}" in first_payload
    assert db.get_task(task["id"]) is not None
    second = handle_command_text(f"confirm {token}", chat_id="chat-delete")
    second_payload = json.dumps(second["card"], ensure_ascii=False)
    assert second["type"] == "interactive"
    assert "任务已删除" in second_payload
    assert f"#{task['id']}" in second_payload
    assert db.get_task(task["id"]) is None


def test_feishu_batch_cancel_task_command_returns_summary_card(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    first = db.create_task(
        project="demo",
        title="批量取消任务 1",
        content="验证飞书批量取消",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    second = db.create_task(
        project="demo",
        title="批量取消任务 2",
        content="验证飞书批量取消",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    reply = handle_command_text(f"cancel {first['id']} {second['id']}")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "批量取消完成" in payload
    assert "成功" in payload
    assert db.get_task(first["id"])["status"] == "cancelled"
    assert db.get_task(second["id"])["status"] == "cancelled"


def test_feishu_batch_archive_task_command_accepts_comma_separated_ids(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    first = db.create_task(
        project="demo",
        title="批量归档任务 1",
        content="验证飞书批量归档",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    second = db.create_task(
        project="demo",
        title="批量归档任务 2",
        content="验证飞书批量归档",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.update_task(first["id"], status="done")
    db.update_task(second["id"], status="done")

    reply = handle_command_text(f"archive {first['id']},{second['id']}")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "批量归档完成" in payload
    assert db.get_task(first["id"])["status"] == "archived"
    assert db.get_task(second["id"])["status"] == "archived"


def test_feishu_batch_delete_task_command_reports_partial_failures(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    deletable = db.create_task(
        project="demo",
        title="批量删除任务 1",
        content="验证飞书批量删除",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    blocked = db.create_task(
        project="demo",
        title="批量删除任务 2",
        content="验证飞书批量删除",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.update_task(deletable["id"], status="done")
    db.update_task(blocked["id"], status="in_progress")

    first = handle_command_text(f"delete {deletable['id']} {blocked['id']}", chat_id="chat-batch-delete")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    pending = db.get_service_state("feishu_confirm", "chat-batch-delete")
    token = pending["meta"]["token"]

    assert first["type"] == "interactive"
    assert "敏感操作待确认" in first_payload
    assert "批量删除 2 个任务" in first_payload
    assert db.get_task(deletable["id"]) is not None
    second = handle_command_text(f"confirm {token}", chat_id="chat-batch-delete")
    second_payload = json.dumps(second["card"], ensure_ascii=False)
    assert second["type"] == "interactive"
    assert "批量删除完成" in second_payload
    assert "失败 1" in second_payload
    assert db.get_task(deletable["id"]) is None
    assert db.get_task(blocked["id"])["status"] == "in_progress"


def test_feishu_natural_language_status_routes_to_project_overview(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    reply = handle_command_text("看看 demo 项目的运行状态")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "项目总览" in payload
    assert "demo" in payload


def test_feishu_natural_language_delete_uses_numbered_choice(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    first = db.create_task(
        project="demo",
        title="自然语言删除任务 1",
        content="验证飞书候选选择",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.create_task(
        project="demo",
        title="自然语言删除任务 2",
        content="验证飞书候选选择",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    handle_command_text("use demo", chat_id="chat-nl-delete")
    first_reply = handle_command_text("删除任务", chat_id="chat-nl-delete")
    second_reply = handle_command_text("1", chat_id="chat-nl-delete")
    pending = db.get_service_state("feishu_confirm", "chat-nl-delete")
    token = pending["meta"]["token"]
    third_reply = handle_command_text(f"confirm {token}", chat_id="chat-nl-delete")
    first_payload = json.dumps(first_reply["card"], ensure_ascii=False)
    second_payload = json.dumps(second_reply["card"], ensure_ascii=False)
    third_payload = json.dumps(third_reply["card"], ensure_ascii=False)

    assert "请确认操作" in first_payload
    assert "回复数字继续" in first_payload
    assert "敏感操作待确认" in second_payload
    assert "confirm" in second_payload
    assert "任务已删除" in third_payload
    assert db.get_task(first["id"]) is None


def test_feishu_pending_choice_out_of_range_keeps_options(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="待选择删除任务",
        content="验证数字越界不会清空待选项",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.create_task(
        project="demo",
        title="另一个待选择任务",
        content="验证数字越界不会清空待选项",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    handle_command_text("use demo", chat_id="chat-nl-out-of-range")
    first_reply = handle_command_text("删除任务", chat_id="chat-nl-out-of-range")
    invalid_reply = handle_command_text("9", chat_id="chat-nl-out-of-range")
    valid_reply = handle_command_text("1", chat_id="chat-nl-out-of-range")
    first_payload = json.dumps(first_reply["card"], ensure_ascii=False)
    invalid_payload = json.dumps(invalid_reply["card"], ensure_ascii=False)
    valid_payload = json.dumps(valid_reply["card"], ensure_ascii=False)

    assert "请确认操作" in first_payload
    assert "可选项超出范围" in invalid_payload
    assert "回复数字继续" in invalid_payload
    assert "敏感操作待确认" in valid_payload


def test_feishu_cancel_confirm_token_keeps_task_unchanged(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="取消确认测试任务",
        content="验证 cancel confirm",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    handle_command_text(f"delete {task['id']}", chat_id="chat-cancel-confirm")
    pending = db.get_service_state("feishu_confirm", "chat-cancel-confirm")
    token = pending["meta"]["token"]
    reply = handle_command_text(f"cancel confirm {token}", chat_id="chat-cancel-confirm")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    confirm_reply = handle_command_text(f"confirm {token}", chat_id="chat-cancel-confirm")
    confirm_payload = json.dumps(confirm_reply["card"], ensure_ascii=False)

    assert "确认已取消" in payload
    assert db.get_task(task["id"]) is not None
    assert "确认已失效" in confirm_payload
    assert db.get_task(task["id"]) is not None


def test_feishu_confirm_rejects_chat_mismatch_and_expired_token(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="确认上下文测试任务",
        content="验证 chat mismatch / expired",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    handle_command_text(f"delete {task['id']}", chat_id="chat-a")
    pending = db.get_service_state("feishu_confirm", "chat-a")
    token = pending["meta"]["token"]

    mismatch = handle_command_text(f"confirm {token}", chat_id="chat-b")
    mismatch_payload = json.dumps(mismatch["card"], ensure_ascii=False)
    assert "确认已失效" in mismatch_payload
    assert db.get_task(task["id"]) is not None

    pending["meta"]["expires_at"] = (datetime.now() - timedelta(seconds=5)).isoformat(timespec="seconds")
    db.upsert_service_state("feishu_confirm", "chat-a", pid=0, status="pending", log_path="", meta=pending["meta"])
    expired = handle_command_text(f"confirm {token}", chat_id="chat-a")
    expired_payload = json.dumps(expired["card"], ensure_ascii=False)

    assert "确认已失效" in expired_payload
    assert db.get_task(task["id"]) is not None


def test_feishu_project_delete_requires_confirm_token(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="删除项目测试任务",
        content="验证 project delete",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    first = handle_command_text("project delete demo", chat_id="chat-project-delete")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    pending = db.get_service_state("feishu_confirm", "chat-project-delete")
    token = pending["meta"]["token"]
    second = handle_command_text(f"confirm {token}", chat_id="chat-project-delete")
    second_payload = json.dumps(second["card"], ensure_ascii=False)

    assert "敏感操作待确认" in first_payload
    assert "工作目录不会被删除" in first_payload
    assert "项目已删除" in second_payload
    assert db.get_project("demo") is None
    assert project_path.exists()


def test_feishu_project_info_card_shows_path_stats_and_service_state(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    done = db.create_task(
        project="demo",
        title="已完成项目任务",
        content="验证项目信息卡片",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )
    db.update_task(done["id"], status="done", completed_at=datetime.now().isoformat())

    reply = handle_command_text("project info demo")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    registered_path = db.get_project("demo")["path"].replace("\\", "\\\\")

    assert reply["type"] == "interactive"
    assert "CodePilot 项目信息" in payload
    assert registered_path in payload
    assert "任务总数" in payload
    assert "完成" in payload
    assert "任务轮询" in payload
    assert "未运行" in payload
    assert "project delete demo" in payload
    assert "detail <id>" not in payload
    assert "logs <id>" not in payload


def test_feishu_project_add_registers_and_refreshes_project_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    project_path = tmp_path / "my project"
    project_path.mkdir()

    created = handle_command_text(f'project add remote "{project_path}"', chat_id="chat-project-add")
    created_payload = json.dumps(created["card"], ensure_ascii=False)
    project = db.get_project("remote")

    assert created["type"] == "interactive"
    assert "项目已注册" in created_payload
    assert project is not None
    assert project["path"] == str(project_path.resolve())
    assert Path(project["config_file"]).exists()

    config_file = project["config_file"]
    db.register_project("remote", str(project_path.resolve()), config_file=None)
    updated = handle_command_text(f'project add remote "{project_path}"', chat_id="chat-project-add")
    updated_payload = json.dumps(updated["card"], ensure_ascii=False)
    refreshed = db.get_project("remote")

    assert "项目已更新" in updated_payload
    assert refreshed["config_file"] == config_file


def test_feishu_project_commands_return_clear_error_cards(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    another_path = tmp_path / "another"
    another_path.mkdir()
    missing_path = tmp_path / "missing"

    invalid = handle_command_text(f'project add bad "{missing_path}"')
    duplicate = handle_command_text(f'project add demo "{another_path}"')
    missing = handle_command_text("project info missing-project")
    invalid_payload = json.dumps(invalid["card"], ensure_ascii=False)
    duplicate_payload = json.dumps(duplicate["card"], ensure_ascii=False)
    missing_payload = json.dumps(missing["card"], ensure_ascii=False)

    assert invalid["type"] == "interactive"
    assert "项目注册失败" in invalid_payload
    assert "不存在或不是目录" in invalid_payload

    assert duplicate["type"] == "interactive"
    assert "项目注册失败" in duplicate_payload
    assert "已注册到" in duplicate_payload

    assert missing["type"] == "interactive"
    assert "项目信息不可用" in missing_payload
    assert "未注册" in missing_payload


def test_feishu_unknown_plain_text_defaults_to_chat_goal(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "codepilot.webapp.actions.submit_goal_action",
        lambda project, text, **kwargs: calls.append({"project": project, "text": text, "kwargs": kwargs}) or {
            "ok": True,
            "intent": "question",
            "message": "这里按 chat 问答处理。",
        },
    )

    reply = handle_command_text("wat")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert calls == [{"project": "demo", "text": "wat", "kwargs": {}}]
    assert "按 chat 问答处理" in payload


def test_feishu_plain_text_without_active_project_prompts_project_choice(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    other_path = tmp_path / "other"
    other_path.mkdir()
    db.register_project("other", str(other_path))

    reply = handle_command_text("帮我看看最近进度", chat_id="chat-multi")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "先确认你要在哪个项目里继续" in payload
    assert "回复数字继续" in payload


def test_feishu_event_payload_dedupes_same_message_id(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    monkeypatch.setattr(
        "codepilot.feishu_bot.handle_command_text",
        lambda text, **kwargs: calls.append({"text": text, "kwargs": kwargs}) or {
            "type": "text",
            "text": "ok",
        },
    )

    payload = {"chat_id": "chat-dup", "message_id": "msg-1", "text": "hello"}
    first = handle_event_payload(payload)
    second = handle_event_payload(payload)

    assert first["type"] == "text"
    assert second == {"type": "ignore"}
    assert calls == [{"text": "hello", "kwargs": {"config_path": None, "chat_id": "chat-dup"}}]


def test_feishu_event_payload_prefers_event_id_for_dedup(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    monkeypatch.setattr(
        "codepilot.feishu_bot.handle_command_text",
        lambda text, **kwargs: calls.append({"text": text, "kwargs": kwargs}) or {
            "type": "text",
            "text": "ok",
        },
    )

    payload = {"chat_id": "chat-dup", "event_id": "evt-1", "message_id": "msg-1", "text": "hello"}
    first = handle_event_payload(payload)
    second = handle_event_payload(payload)

    assert first["type"] == "text"
    assert second == {"type": "ignore"}
    assert calls == [{"text": "hello", "kwargs": {"config_path": None, "chat_id": "chat-dup"}}]


def test_feishu_event_payload_failure_keeps_dedup_record(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "codepilot.feishu_bot.handle_command_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    payload = {"chat_id": "chat-dup", "event_id": "evt-fail-1", "message_id": "msg-fail-1", "text": "hello"}
    first = handle_event_payload(payload)
    second = handle_event_payload(payload)
    state = db.get_service_state("feishu_inbound_msg", "event:evt-fail-1")

    assert first["type"] == "interactive"
    assert second == {"type": "ignore"}
    assert state
    assert state["status"] == "failed"


def test_feishu_handle_event_cli_emits_clean_json_even_with_stdout_noise(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    def _noisy_handler(payload, **kwargs):
        print("noise on stdout")
        return {"type": "text", "text": "ok"}

    monkeypatch.setattr(feishu_cmd, "handle_event_payload", _noisy_handler)

    runner = CliRunner()
    result = runner.invoke(
        feishu_cmd.handle_event_cmd,
        input=json.dumps({"chat_id": "chat-1", "message_id": "msg-1", "text": "hello"}, ensure_ascii=False),
    )

    assert result.exit_code == 0
    last_line = [line for line in result.output.splitlines() if line.strip()][-1]
    assert json.loads(last_line) == {"type": "text", "text": "ok"}


def test_submit_goal_action_without_webui_server_uses_fallback_ui_state(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    import sys
    from codepilot.webapp import actions as web_actions

    monkeypatch.setitem(sys.modules, "codepilot.webapp.server", None)
    web_actions._UI_JOB_SEQ = 0
    web_actions._UI_JOBS.clear()
    web_actions._UI_EVENTS.clear()

    monkeypatch.setattr(
        "codepilot.webapp.actions.assess_requirement_for_planning",
        lambda title, **kwargs: {"status": "ready", "refined_title": title},
    )
    monkeypatch.setattr(
        web_actions,
        "run_requirement_workflow",
        lambda **kwargs: {"tasks": [], "run": {"done": 0, "failed": 0}, "summary": "ok"},
    )

    result = web_actions.submit_goal_action("demo", "帮我整理任务说明", category="requirement")

    assert result["ok"] is True
    assert result["job"]["id"] == 1
    assert result["job"]["status"] in {"queued", "running", "succeeded", "attention"}


def test_feishu_req_new_creates_requirement_session_with_context_commands(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    planned = []

    monkeypatch.setattr(
        "codepilot.webapp.actions.assess_requirement_for_planning",
        lambda title, **kwargs: {"status": "ready", "refined_title": title},
    )

    def fake_submit(project, text, **kwargs):
        planned.append({"project": project, "text": text, "kwargs": kwargs})
        return {"ok": True, "message": "queued", "job": {"task_ids": [21]}}

    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    reply = handle_command_text("req new 优化飞书任务卡片", chat_id="chat-session-new")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    sessions = db.list_sessions(project="demo")

    assert reply["type"] == "interactive"
    assert len(sessions) == 1
    assert planned[0]["project"] == "demo"
    assert planned[0]["text"] == "优化飞书任务卡片"
    assert "session reply 1 <text>" in payload
    assert "detail 21" in payload
    assert "logs 21" in payload
    assert "daemon start" not in payload


def test_feishu_req_new_clarify_card_uses_session_reply_command(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "codepilot.webapp.actions.assess_requirement_for_planning",
        lambda title, **kwargs: {
            "status": "needs_clarification",
            "questions": [{"id": "q1", "type": "text", "text": "先做哪个入口?", "options": []}],
            "qa_history": [],
        },
    )

    reply = handle_command_text("req new 优化控制入口", chat_id="chat-session-clarify")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "需求会话待继续" in payload
    assert "先做哪个入口" in payload
    assert "session reply 1 <你的补充信息>" in payload
    assert "答 <内容>" not in payload


def test_feishu_session_reply_continues_pending_session(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "codepilot.webapp.actions.assess_requirement_for_planning",
        lambda title, **kwargs: {
            "status": "needs_clarification",
            "questions": [{"id": "q1", "type": "text", "text": "先做哪个入口?", "options": []}],
            "qa_history": [],
        },
    )
    first = handle_command_text("req new 优化控制入口", chat_id="chat-session-reply")
    assert first["type"] == "interactive"

    planned = []

    monkeypatch.setattr(
        "codepilot.webapp.actions.continue_pending_clarification",
        lambda *args, **kwargs: {"status": "ready", "refined_title": "优化控制入口 / 先做飞书"},
    )

    def fake_submit(project, text, **kwargs):
        planned.append({"project": project, "text": text, "kwargs": kwargs})
        return {"ok": True, "message": "queued", "job": {"task_ids": [31]}}

    monkeypatch.setattr("codepilot.webapp.actions.submit_requirement_action", fake_submit)

    reply = handle_command_text("session reply 1 先做飞书", chat_id="chat-session-reply")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    messages = db.list_session_messages(1)

    assert reply["type"] == "interactive"
    assert planned[0]["project"] == "demo"
    assert planned[0]["text"] == "优化控制入口 / 先做飞书"
    assert len(messages) == 4
    assert "需求会话已提交" in payload
    assert "优化控制入口 / 先做飞书" in payload
    assert "detail 31" in payload
    assert "logs 31" in payload


def test_feishu_session_reply_validates_args_and_missing_session(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="请提供会话 ID 和内容"):
        handle_command_text("session reply 12")

    with pytest.raises(RuntimeError, match="会话 #999 不存在"):
        handle_command_text("session reply 999 先做飞书")


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
