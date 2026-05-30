from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from codepilot.commands import feishu as feishu_cmd
from codepilot.feishu_config import FEISHU_CONFIG_REF_ENV, load_feishu_bot_config
from codepilot.feishu_bot import card_builders
from codepilot.feishu_bot import handle_command_text
from codepilot.storage import database as db
from tests.feishu_bot_testkit import _setup_project


def _stub_opencode(monkeypatch, calls: list[dict], *, message: str = "OpenCode 已处理。"):
    def fake_run(project, text, *, source, external_session_id):
        calls.append({"project": project, "text": text, "source": source, "external_session_id": external_session_id})
        return {"ok": True, "message": message, "opencode_session_id": f"ses-{len(calls)}"}

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)


def _write_feishu_config(
    path: Path,
    *,
    enabled: bool = True,
    app_id: str = "cli-target",
    app_secret: str = "secret-target",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
[project]
name = "demo"

[feishu_bot]
enabled = {str(enabled).lower()}
app_id = "{app_id}"
app_secret = "{app_secret}"
default_project = "demo"
""".strip(),
        encoding="utf-8",
    )


def test_feishu_req_new_enters_opencode_without_old_requirement_session(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 已接管 req new。")

    reply = handle_command_text("req new 优化飞书任务卡片", chat_id="chat-session-new")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    sessions = db.list_sessions(project="demo")

    assert reply["type"] == "interactive"
    assert len(sessions) == 1
    assert calls == [
        {
            "project": "demo",
            "text": "优化飞书任务卡片",
            "source": "feishu",
            "external_session_id": str(sessions[0]["id"]),
        }
    ]
    assert "OpenCode 已接管 req new" in payload
    assert "session reply 1 <text>" not in payload

def test_feishu_req_new_no_longer_returns_old_clarify_card(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 会在会话里继续追问。")

    reply = handle_command_text("req new 优化控制入口", chat_id="chat-session-clarify")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert calls[0]["text"] == "优化控制入口"
    assert "OpenCode 会在会话里继续追问" in payload
    assert "需求会话待继续" not in payload
    assert "session reply 1 <你的补充信息>" not in payload

def test_feishu_session_reply_compatibility_routes_to_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    session = db.create_session("demo", "飞书补充")
    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 收到补充信息。")

    reply = handle_command_text(f"session reply {session['id']} 先做飞书", chat_id="chat-session-reply")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert calls == [
        {"project": "demo", "text": "先做飞书", "source": "feishu", "external_session_id": str(session["id"])}
    ]
    assert "OpenCode 收到补充信息" in payload

def test_feishu_session_reply_validates_args_and_missing_session(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="请提供会话 ID 和内容"):
        handle_command_text("session reply 12")

    calls = []
    _stub_opencode(monkeypatch, calls, message="OpenCode 收到补充信息。")

    with pytest.raises(RuntimeError, match="会话 #999 不存在"):
        handle_command_text("session reply 999 先做飞书", chat_id="chat-missing-session")
    assert calls == []

def test_ensure_feishu_service_running_if_enabled_returns_disabled_when_config_off(monkeypatch):
    monkeypatch.setattr(
        feishu_cmd,
        "load_feishu_bot_config",
        lambda: type("Cfg", (), {"enabled": False})(),
    )

    result = feishu_cmd.ensure_service_running_if_enabled()

    assert result == {"enabled": False, "running": False, "started": False}

def test_ensure_feishu_service_running_if_enabled_uses_registered_project_config_from_foreign_cwd(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    project_path = tmp_path / "project"
    foreign_cwd = tmp_path / "elsewhere"
    config_file = tmp_path / "config-root" / "AGENTS.toml"
    project_path.mkdir()
    foreign_cwd.mkdir()
    _write_feishu_config(config_file)
    project_info = db.register_project("demo", str(project_path), config_file=str(config_file))
    monkeypatch.chdir(foreign_cwd)

    checked_refs: list[str | None] = []
    spawned_refs: list[str | None] = []

    def fake_check_runtime_ready(config_ref=None):
        checked_refs.append(str(config_ref) if config_ref else None)
        cfg = load_feishu_bot_config(config_ref)
        assert cfg.enabled is True
        assert cfg.app_id == "cli-target"
        assert cfg.app_secret == "secret-target"

    class _Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_spawn_detached(config_ref=None):
        spawned_refs.append(str(config_ref) if config_ref else None)
        return _Proc()

    monkeypatch.setattr(feishu_cmd, "_check_runtime_ready", fake_check_runtime_ready)
    monkeypatch.setattr(feishu_cmd, "_service_status", lambda: {"running": False})
    monkeypatch.setattr(feishu_cmd, "_clear_state", lambda: None)
    monkeypatch.setattr(feishu_cmd, "_stop_feishu_processes", lambda: [])
    monkeypatch.setattr(feishu_cmd, "_spawn_detached", fake_spawn_detached)
    monkeypatch.setattr(feishu_cmd, "LOG_FILE", tmp_path / "feishu.log")
    monkeypatch.setattr(feishu_cmd.time, "sleep", lambda _seconds: None)

    result = feishu_cmd.ensure_service_running_if_enabled(project_info)

    assert result["started"] is True
    assert checked_refs == [str(config_file)]
    assert spawned_refs == [str(config_file)]

def test_feishu_spawn_detached_passes_config_reference_in_env_not_command(monkeypatch, tmp_path):
    calls = []
    config_file = tmp_path / "config-root" / "AGENTS.toml"
    _write_feishu_config(config_file)
    monkeypatch.setattr(feishu_cmd, "STATE_DIR", tmp_path / "feishu")
    monkeypatch.setattr(feishu_cmd, "LOG_FILE", tmp_path / "feishu.log")
    monkeypatch.setattr(feishu_cmd, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        feishu_cmd,
        "spawn_detached_command_via_launcher",
        lambda cmd, *, log_file, cwd=None, env=None: calls.append((cmd, log_file, cwd, env)) or 4321,
    )

    proc = feishu_cmd._spawn_detached(str(config_file))

    assert proc.pid == 4321
    cmd, log_file, cwd, env = calls[0]
    assert cmd == [feishu_cmd.sys.executable, "-m", "codepilot", "feishu", "run"]
    assert str(config_file) not in " ".join(cmd)
    assert env[FEISHU_CONFIG_REF_ENV] == str(config_file)
    assert env.get("CODEPILOT_FEISHU_APP_SECRET") != "secret-target"
    assert log_file == tmp_path / "feishu.log"
    assert cwd == tmp_path

def test_feishu_worker_env_and_command_use_config_reference(monkeypatch, tmp_path):
    config_file = tmp_path / "config-root" / "AGENTS.toml"
    _write_feishu_config(config_file)

    env = feishu_cmd._build_worker_env(str(config_file))
    command = feishu_cmd._worker_command(str(config_file))

    assert command == [sys.executable, "-m", "codepilot", "feishu", "run-worker"]
    assert env[FEISHU_CONFIG_REF_ENV] == str(config_file)
    assert env["CODEPILOT_FEISHU_APP_ID"] == "cli-target"
    assert env["CODEPILOT_FEISHU_APP_SECRET"] == "secret-target"

def test_feishu_config_loader_uses_env_config_reference_from_foreign_cwd(tmp_path, monkeypatch):
    config_file = tmp_path / "config-root" / "AGENTS.toml"
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()
    _write_feishu_config(config_file, app_id="env-target", app_secret="env-secret")
    monkeypatch.chdir(foreign_cwd)
    monkeypatch.setenv(FEISHU_CONFIG_REF_ENV, str(config_file))

    cfg = load_feishu_bot_config()

    assert cfg.enabled is True
    assert cfg.app_id == "env-target"
    assert cfg.app_secret == "env-secret"

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
    assert feishu_cmd._is_feishu_process_command("python.exe -m codepilot feishu run-worker")
    assert feishu_cmd._is_feishu_process_command(
        "python.exe D:\\myCode\\workflow\\codepilot\\feishu_worker.py"
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
        lambda cmd, *, log_file, cwd=None, env=None: calls.append((cmd, log_file, cwd, env)) or 4321,
    )

    proc = feishu_cmd._spawn_detached()

    assert proc.pid == 4321
    assert calls
    cmd, log_file, cwd, env = calls[0]
    assert cmd == [feishu_cmd.sys.executable, "-m", "codepilot", "feishu", "run"]
    assert log_file == tmp_path / "feishu.log"
    assert cwd == tmp_path
    assert env is None

def test_feishu_spawn_detached_uses_binary_command_when_frozen(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(feishu_cmd.sys, "frozen", True, raising=False)
    monkeypatch.setattr(feishu_cmd.sys, "executable", r"C:\Tools\CodePilot\codepilot.exe")
    monkeypatch.setattr(feishu_cmd, "STATE_DIR", tmp_path / "feishu")
    monkeypatch.setattr(feishu_cmd, "LOG_FILE", tmp_path / "feishu.log")
    monkeypatch.setattr(feishu_cmd, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        feishu_cmd,
        "spawn_detached_command_via_launcher",
        lambda cmd, *, log_file, cwd=None, env=None: calls.append((cmd, log_file, cwd, env)) or 4321,
    )

    proc = feishu_cmd._spawn_detached()

    assert proc.pid == 4321
    assert calls[0][0] == [r"C:\Tools\CodePilot\codepilot.exe", "feishu", "run"]

def test_feishu_runtime_uses_install_dir_when_frozen(monkeypatch, tmp_path):
    install_dir = tmp_path / "bin"
    install_dir.mkdir(parents=True)

    monkeypatch.setattr(feishu_cmd.sys, "frozen", True, raising=False)
    monkeypatch.setattr(feishu_cmd.sys, "executable", str(install_dir / "codepilot.exe"))

    assert feishu_cmd._repo_root() == install_dir
    assert feishu_cmd._worker_script().name == "feishu_worker.py"
    assert card_builders._feishu_notify_script().name == "feishu_notify.py"
