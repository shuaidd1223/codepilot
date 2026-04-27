from __future__ import annotations

import json

from codepilot.commands import feishu as feishu_cmd
from codepilot.feishu_bot import handle_command_text
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
    assert "stop 123" in payload


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
