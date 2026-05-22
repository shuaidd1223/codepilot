"""Focused tests for chat command dispatch helpers."""

from __future__ import annotations

from types import SimpleNamespace

from codepilot.commands.auto_chat_commands import (
    _dispatch_chat_natural_language_command,
)
from codepilot.commands.inspect_workflow import write_inspect_workflow_context
from codepilot.storage import database as db
from tests.chat_flow_testkit import register_project


def test_chat_command_dispatch_module_executes_project_task_status(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    called = {}
    runtime = SimpleNamespace(
        project_info={"name": "demo", "is_temporary": False},
        pending_action_options=[],
        shell=SimpleNamespace(
            render_project_dashboard=lambda *args, **kwargs: called.update({
                "args": args,
                "kwargs": kwargs,
            }),
        ),
    )

    handled, response = _dispatch_chat_natural_language_command(
        "看看 demo 项目的任务状态",
        runtime,
        echo=lambda *_args, **_kwargs: None,
    )

    assert handled is True
    assert response == "已显示 demo 任务面板"
    assert called["args"] == ("demo",)
    assert "任务面板" in called["kwargs"]["title"]


def test_chat_command_dispatch_renders_inspect_workflow_next_actions(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
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
        session_id="inspect-chat",
    )
    runtime = SimpleNamespace(
        project_info={"name": "demo", "is_temporary": False},
        pending_action_options=[],
        shell=SimpleNamespace(),
    )

    handled, response = _dispatch_chat_natural_language_command(
        "看一下巡检建议",
        runtime,
        echo=lambda *_args, **_kwargs: None,
    )

    assert handled is True
    assert "巡检工作流" in response
    assert "report_only=1" in response
    assert "workflow next demo auto" in response
    assert "promote_inspect_report_inspect-report" in response
    assert "ignore_inspect_report_inspect-report" in response
    assert "archive_inspect_report_inspect-report" in response
    assert "delete_inspect_report_inspect-report" in response
