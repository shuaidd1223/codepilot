from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from codepilot.commands import feishu as feishu_cmd
from codepilot.feishu_bot import handle_command_text
from codepilot.storage import database as db
from tests.feishu_bot_testkit import _setup_project


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

def test_feishu_process_command_detection_matches_only_feishu_processes():
    assert feishu_cmd._is_feishu_process_command("python.exe -m codepilot feishu run")
    assert feishu_cmd._is_feishu_process_command("node D:\\myCode\\workflow\\codepilot\\feishu_worker.mjs")
    assert feishu_cmd._is_feishu_process_command(
        "cmd.exe /C D:\\ServBay\\bin\\node.cmd D:\\myCode\\workflow\\codepilot\\feishu_worker.mjs"
    )
    assert not feishu_cmd._is_feishu_process_command("python.exe -m codepilot daemon --project demo")
    assert not feishu_cmd._is_feishu_process_command("python.exe -m codepilot ui --port 8766")

def test_ensure_feishu_service_running_if_enabled_cleans_orphan_workers_before_start(monkeypatch, tmp_path):
    cleaned = []
    monkeypatch.setattr(
        feishu_cmd,
        "load_feishu_bot_config",
        lambda: type("Cfg", (), {"enabled": True})(),
    )
    monkeypatch.setattr(feishu_cmd, "_check_runtime_ready", lambda: None)
    monkeypatch.setattr(feishu_cmd, "_service_status", lambda: {"running": False})
    monkeypatch.setattr(feishu_cmd, "_clear_state", lambda: None)
    monkeypatch.setattr(feishu_cmd, "_stop_feishu_processes", lambda: cleaned.append(True) or [31740])
    monkeypatch.setattr(feishu_cmd, "LOG_FILE", tmp_path / "feishu.log")
    monkeypatch.setattr(feishu_cmd.time, "sleep", lambda _seconds: None)

    class _Proc:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(feishu_cmd, "_spawn_detached", lambda: _Proc())

    result = feishu_cmd.ensure_service_running_if_enabled()

    assert cleaned == [True]
    assert result["started"] is True

def test_feishu_stop_cmd_cleans_orphan_workers(monkeypatch):
    calls = []
    monkeypatch.setattr(feishu_cmd, "_service_status", lambda: {"pid": 0})
    monkeypatch.setattr(feishu_cmd, "_clear_state", lambda: calls.append("clear"))
    monkeypatch.setattr(feishu_cmd, "_stop_feishu_processes", lambda: calls.append("orphans") or [31740])

    runner = CliRunner()
    result = runner.invoke(feishu_cmd.stop_cmd)

    assert result.exit_code == 0
    assert calls == ["orphans", "clear"]

def test_feishu_spawn_detached_uses_external_launcher(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(feishu_cmd, "STATE_DIR", tmp_path / "feishu")
    monkeypatch.setattr(feishu_cmd, "LOG_FILE", tmp_path / "feishu.log")
    monkeypatch.setattr(feishu_cmd, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        feishu_cmd,
        "spawn_detached_command_via_launcher",
        lambda cmd, *, log_file, cwd=None: calls.append((cmd, log_file, cwd)) or 4321,
    )

    proc = feishu_cmd._spawn_detached()

    assert proc.pid == 4321
    assert calls
    cmd, log_file, cwd = calls[0]
    assert cmd == [feishu_cmd.sys.executable, "-m", "codepilot", "feishu", "run"]
    assert log_file == tmp_path / "feishu.log"
    assert cwd == tmp_path
