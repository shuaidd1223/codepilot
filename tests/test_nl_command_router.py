"""Unit tests for natural-language command routing stages."""

from __future__ import annotations

from codepilot.nl_command_router import (
    _detect_task_action,
    _service_action_from_text,
    _task_ids_command,
    resolve_natural_language_command,
)
from codepilot.storage import database as db
from tests.chat_flow_testkit import register_project


def test_service_action_from_text_detects_control_verbs():
    assert _service_action_from_text("启动巡检") == "start"
    assert _service_action_from_text("停掉任务执行服务") == "stop"
    assert _service_action_from_text("看看任务轮询") == "status"


def test_detect_task_action_keeps_existing_priority_order():
    assert _detect_task_action("查看任务日志输出") == "logs"
    assert _detect_task_action("重新执行任务 7") == "retry"
    assert _detect_task_action("这个项目的任务状态") is None


def test_task_ids_command_batches_destructive_multi_id_actions():
    assert _task_ids_command("delete", [1, 2, 3]) == {
        "status": "match",
        "command": "delete 1 2 3",
        "label": "批量删除任务",
    }
    assert _task_ids_command("retry", [7, 8]) == {
        "status": "match",
        "command": "retry 7",
        "label": "retry 任务 #7",
    }


def test_resolve_natural_language_command_routes_through_split_resolvers(tmp_path, monkeypatch):
    project_path = register_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="需要查看日志的任务",
        content="验证自然语言任务动作分发",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    assert resolve_natural_language_command("查看 demo 项目状态") == {
        "status": "match",
        "command": "overview demo",
        "label": "查看 demo 项目状态",
    }
    assert resolve_natural_language_command("停止任务执行服务", active_project="demo") == {
        "status": "match",
        "command": "daemon stop demo",
        "label": "stop 任务执行服务",
    }
    assert resolve_natural_language_command(f"查看任务 {task['id']} 的日志", active_project="demo") == {
        "status": "match",
        "command": f"logs {task['id']}",
        "label": f"logs 任务 #{task['id']}",
    }

