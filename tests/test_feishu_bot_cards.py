from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from codepilot.feishu_bot import (
    build_batch_task_action_card,
    build_pending_confirm_card,
    build_task_event_card,
    handle_command_text,
    notify_feishu_task_event,
)
from codepilot.storage import database as db
from tests.feishu_bot_testkit import (
    _assert_card_uses_markdown,
    _assert_multi_button_actions_use_flow_layout,
    _assert_no_copy_command_panel,
    _button_commands,
    _button_types_by_command,
    _setup_project,
)


def test_feishu_help_returns_interactive_card(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    reply = handle_command_text("help")

    assert reply["type"] == "interactive"
    assert "CodePilot 飞书命令" in reply["card"]["header"]["title"]["content"]
    assert {"global", "projects", "tasks"}.issubset(set(_button_commands(reply["card"])))
    _assert_no_copy_command_panel(reply["card"])
    _assert_card_uses_markdown(reply["card"])
    _assert_multi_button_actions_use_flow_layout(reply["card"])

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
    assert "快捷操作" in payload
    assert "重点任务" in payload
    assert "任务操作" in payload
    assert "查看详情" in payload
    assert "任务日志" in payload
    assert "删除任务" in payload
    assert {"detail 1", "logs 1", "delete 1", "tasks demo status=backlog"}.issubset(set(_button_commands(reply["card"])))
    _assert_no_copy_command_panel(reply["card"])
    _assert_multi_button_actions_use_flow_layout(reply["card"])
    assert any(
        elem.get("tag") == "action" and any(action.get("tag") == "button" for action in elem.get("actions", []))
        for elem in reply["card"]["elements"]
        if isinstance(elem, dict)
    )


def test_feishu_workflow_card_lists_inspect_next_actions(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    from codepilot.commands.inspect_workflow import write_inspect_workflow_context

    write_inspect_workflow_context(
        db.get_project("demo"),
        {
            "project": "demo",
            "created": [],
            "report_only": [
                {
                    "candidate_id": "inspect-report",
                    "title": "报告 foo.py 线索",
                    "goal": "人工评估 foo.py。",
                    "priority": "P4",
                    "reason": "priority_p4_report_only",
                    "files": ["foo.py"],
                    "evidence": "signal 3: foo.py",
                }
            ],
            "dropped": [],
            "skipped": [],
            "quality_summary": {"created_count": 0, "report_only_count": 1},
        },
        source_command="codepilot inspect -p demo --once --dry-run --write-workflow --json",
        session_id="inspect-feishu",
    )

    reply = handle_command_text("workflow demo")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "CodePilot 工作流下一步" in payload
    assert "报告 foo.py 线索" in payload
    assert "promote_inspect_report_inspect-report" in payload
    assert "workflow next demo promote_inspect_report_inspect-report" in set(_button_commands(reply["card"]))

def test_feishu_tasks_card_uses_visual_task_rows_instead_of_plain_text_only(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    running = db.create_task(
        project="demo",
        title="执行中的专业任务卡片",
        content="验证任务行视觉层级",
        agent="codex",
        priority="P0",
        project_path=str(project_path),
    )
    db.update_task(running["id"], status="in_progress", run_phase="builder")

    reply = handle_command_text("tasks demo")
    card = reply["card"]
    payload = json.dumps(card, ensure_ascii=False)
    action_blocks = [elem for elem in card["elements"] if isinstance(elem, dict) and elem.get("tag") == "action"]
    buttons = [button for block in action_blocks for button in block.get("actions", []) if isinstance(button, dict)]

    assert "重点任务" in payload
    assert "任务 #1" in payload
    assert "P0" in payload
    assert "执行中" in payload
    assert "任务操作" in payload
    assert any(button.get("type") == "primary" and button.get("value", {}).get("command") == "detail 1" for button in buttons)
    assert any(button.get("type") == "danger" and button.get("value", {}).get("command") == "stop 1" for button in buttons)
    assert not any("**任务**" in elem.get("text", {}).get("content", "") for elem in card["elements"] if isinstance(elem, dict))

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

    monkeypatch.setattr("codepilot.feishu_bot.command_handlers.project_service_action", fake_project_service_action)

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

    assert card["config"]["wide_screen_mode"] is True
    assert card["config"]["enable_forward"] is True
    assert card["config"]["update_multi"] is True
    assert "CodePilot · Build 完成" in card["header"]["title"]["content"]
    assert "note" in tags
    assert "column_set" in tags
    assert tags.count("hr") >= 2
    assert any(elem.get("tag") == "action" for elem in card["elements"] if isinstance(elem, dict))
    _assert_card_uses_markdown(card)
    assert {"detail 12", "logs 12", "tasks"}.issubset(set(_button_commands(card)))
    _assert_no_copy_command_panel(card)

def test_feishu_card_builders_delegate_split_batch_and_event_modules():
    from codepilot.feishu_bot.batch_action_cards import build_batch_task_action_card as split_batch_card
    from codepilot.feishu_bot.notification_cards import build_task_event_card as split_event_card

    batch_result = {
        "total": 2,
        "success_count": 1,
        "failed_count": 1,
        "message": "批量archive完成：成功 1，失败 1。",
        "succeeded": [{"task_id": 21, "message": "任务 #21 已归档。", "task": {"id": 21}}],
        "failed": [{"task_id": 22, "error": "任务 #22 正在执行中，不能归档。"}],
    }
    event_kwargs = {
        "project_name": "demo",
        "task_id": 21,
        "task_title": "拆分飞书卡片构建",
        "event": "phase_end",
        "phase": "builder",
        "level": "info",
        "message": "builder 阶段完成。",
        "status": "in_progress",
    }

    assert split_batch_card("archive", batch_result, prefix="/cp") == build_batch_task_action_card(
        "archive",
        batch_result,
        prefix="/cp",
    )
    assert split_event_card(**event_kwargs) == build_task_event_card(**event_kwargs)

def test_feishu_cards_use_professional_sectioned_layout(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="专业控制台卡片",
        content="验证飞书任务面板布局",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )

    reply = handle_command_text("tasks demo")
    card = reply["card"]
    payload = json.dumps(card, ensure_ascii=False)
    tags = [elem.get("tag") for elem in card["elements"] if isinstance(elem, dict)]

    assert card["config"]["wide_screen_mode"] is True
    assert card["config"]["enable_forward"] is True
    assert card["config"]["update_multi"] is True
    assert "任务概览" in payload
    assert "重点任务" in payload
    assert "快捷操作" in payload
    assert "复制命令发送即可执行" not in payload
    assert {"tasks demo status=backlog", "tasks demo status=in_progress"}.issubset(set(_button_commands(card)))
    _assert_card_uses_markdown(card)
    assert tags.count("hr") >= 2
    assert any(
        elem.get("tag") == "column_set" and elem.get("background_style") == "default"
        for elem in card["elements"]
        if isinstance(elem, dict)
    )

def test_feishu_confirm_card_uses_sectioned_warning_layout():
    card = build_pending_confirm_card(
        {
            "token": "ABC123",
            "action": "delete_task",
            "summary": "删除任务 #12",
            "details": ["- `#12` 专业控制台卡片 / 待执行"],
            "expires_at": "2026-04-29T12:00:00",
        },
        prefix="/cp",
    )
    payload = json.dumps(card, ensure_ascii=False)
    tags = [elem.get("tag") for elem in card["elements"] if isinstance(elem, dict)]

    assert card["config"]["enable_forward"] is True
    assert card["header"]["template"] == "orange"
    assert "请二次确认" in payload
    assert "影响范围" in payload
    assert "确认操作" in payload
    assert _button_types_by_command(card)["/cp confirm ABC123"] == "danger"
    assert _button_types_by_command(card)["/cp cancel confirm ABC123"] == "default"
    assert "`/cp confirm ABC123`" not in payload
    _assert_card_uses_markdown(card)
    assert tags.count("hr") >= 2

def test_feishu_task_notifications_dedupe_same_event(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    def fake_send(card, *, project_name="", chat_ids=None):
        calls.append((card, project_name, chat_ids))
        return True

    monkeypatch.setattr("codepilot.feishu_bot.card_builders._send_bot_card", fake_send)

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
