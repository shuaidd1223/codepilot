"""Focused tests for chat command dispatch helpers."""

from __future__ import annotations

from types import SimpleNamespace

from codepilot.commands.auto_chat_commands import (
    _dispatch_chat_natural_language_command,
)
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
