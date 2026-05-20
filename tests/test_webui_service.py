from __future__ import annotations

import os

from click.testing import CliRunner

from codepilot.storage import database as db
from codepilot.commands import webui_service as svc


def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    state_dir = tmp_path / ".codepilot"
    monkeypatch.setattr(svc, "STATE_DIR", state_dir)
    monkeypatch.setattr(svc, "LOG_FILE", state_dir / "webui.log")
    monkeypatch.setattr(svc, "request_daemon_service_start", lambda project="": {"started": False, "pid": 2468, "project": project})
    monkeypatch.setattr(svc, "_listening_service_pids", lambda: [])
    return state_dir


def test_webui_stop_uses_runtime_process_tree_helper(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    db.upsert_service_state(
        "webui",
        "_global",
        pid=4321,
        status="running",
        log_path=str(svc.LOG_FILE),
        meta={"pid": 4321, "host": "127.0.0.1", "port": 8766},
    )

    alive = {4321: True}
    monkeypatch.setattr(svc, "is_process_alive", lambda pid: alive.get(int(pid), False))

    calls: list[tuple[int, int]] = []

    def fake_stop_process_tree(pid: int, wait_seconds: int = 5) -> bool:
        calls.append((int(pid), int(wait_seconds)))
        alive[int(pid)] = False
        return True

    monkeypatch.setattr(svc, "stop_process_tree", fake_stop_process_tree)

    runner = CliRunner()
    result = runner.invoke(svc.webui, ["stop"])

    assert result.exit_code == 0
    assert calls == [(4321, 5)]
    assert db.get_service_state("webui", "_global") is None
    assert "Web UI 已停止" in result.output


def test_webui_restart_stops_existing_service_before_starting_new_one(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    db.upsert_service_state(
        "webui",
        "_global",
        pid=4321,
        status="running",
        log_path=str(svc.LOG_FILE),
        meta={"pid": 4321, "host": "127.0.0.1", "port": 9988},
    )

    alive = {4321: True}
    monkeypatch.setattr(svc, "is_process_alive", lambda pid: alive.get(int(pid), False))

    calls: list[tuple[int, int]] = []

    def fake_stop_process_tree(pid: int, wait_seconds: int = 5) -> bool:
        calls.append((int(pid), int(wait_seconds)))
        alive[int(pid)] = False
        return True

    class _FakeProc:
        pid = 5678
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(svc, "stop_process_tree", fake_stop_process_tree)
    monkeypatch.setattr(svc, "_spawn_detached", lambda host, port: _FakeProc())
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(svc.webui, ["restart", "-p", "demo"])

    assert result.exit_code == 0
    assert calls == [(4321, 5)]
    state = db.get_service_state("webui", "_global")
    assert state is not None
    meta = state["meta"]
    assert meta["pid"] == 5678
    assert meta["host"] == "127.0.0.1"
    assert meta["port"] == 9988
    assert "Web UI 已启动" in result.output
    assert "项目 demo 任务执行服务已在运行" in result.output


def test_webui_start_ensures_daemon_service(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)

    class _FakeProc:
        pid = 5678
        returncode = None

        def poll(self):
            return None

    calls = []
    monkeypatch.setattr(svc, "_spawn_detached", lambda host, port: _FakeProc())
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)
    monkeypatch.setattr(svc, "request_daemon_service_start", lambda project="": calls.append(project) or {"started": True, "pid": 8765})

    result = CliRunner().invoke(svc.webui, ["start", "--no-open", "-p", "demo"])

    assert result.exit_code == 0
    assert calls == ["demo"]
    assert "Web UI 已启动" in result.output
    assert "项目 demo 任务执行服务已后台启动" in result.output


def test_webui_spawn_command_uses_frozen_executable_without_module_args(monkeypatch):
    monkeypatch.setattr(svc.sys, "executable", r"C:\Tools\CodePilot\codepilot.exe")
    monkeypatch.setattr(svc.sys, "frozen", True, raising=False)

    cmd = svc._foreground_ui_command("127.0.0.1", 9876)

    assert cmd == [
        r"C:\Tools\CodePilot\codepilot.exe",
        "ui",
        "--host",
        "127.0.0.1",
        "--port",
        "9876",
        "--no-open",
    ]
    assert "-m" not in cmd


def test_webui_spawn_detached_resets_pyinstaller_env_for_frozen_parent(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setattr(svc.sys, "frozen", True, raising=False)
    captured = {}

    class _FakeProc:
        pid = 1234
        returncode = None

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(svc.subprocess, "Popen", fake_popen)

    proc = svc._spawn_detached("127.0.0.1", 9876)

    assert proc.pid == 1234
    assert captured["kwargs"]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert captured["kwargs"]["env"]["PATH"] == os.environ["PATH"]


def test_webui_spawn_detached_hides_windows_console_for_frozen_parent(tmp_path, monkeypatch):
    if svc.os.name != "nt":
        return
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setattr(svc.sys, "frozen", True, raising=False)
    monkeypatch.setattr(svc.os, "name", "nt")
    captured = {}

    class _FakeProc:
        pid = 1234
        returncode = None

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(svc.subprocess, "Popen", fake_popen)

    svc._spawn_detached("127.0.0.1", 9876)

    flags = captured["kwargs"]["creationflags"]
    assert flags & svc.CREATE_NO_WINDOW
    assert flags & svc.DETACHED_PROCESS
    assert flags & svc.CREATE_NEW_PROCESS_GROUP
    assert captured["kwargs"]["startupinfo"].wShowWindow == 0


def test_webui_restart_port_env_overrides_stale_meta(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_WEBUI_PORT", "9876")
    db.upsert_service_state(
        "webui",
        "_global",
        pid=4321,
        status="stopped",
        log_path=str(svc.LOG_FILE),
        meta={"pid": 4321, "host": "127.0.0.1", "port": 8766},
    )

    class _FakeProc:
        pid = 5678
        returncode = None

        def poll(self):
            return None

    captured: dict[str, int | str] = {}

    def fake_spawn(host: str, port: int):
        captured["host"] = host
        captured["port"] = port
        return _FakeProc()

    monkeypatch.setattr(svc, "is_process_alive", lambda _pid: False)
    monkeypatch.setattr(svc, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)

    result = CliRunner().invoke(svc.webui, ["restart", "--no-daemon"])

    assert result.exit_code == 0
    assert captured == {"host": "127.0.0.1", "port": 9876}
    assert db.get_service_state("webui", "_global")["meta"]["port"] == 9876


def test_webui_stop_falls_back_to_listener_pid_when_pid_file_is_stale(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    db.upsert_service_state(
        "webui",
        "_global",
        pid=61432,
        status="running",
        log_path=str(svc.LOG_FILE),
        meta={"pid": 61432, "host": "127.0.0.1", "port": 8766},
    )

    alive = {33024: True}
    monkeypatch.setattr(svc, "is_process_alive", lambda pid: alive.get(int(pid), False))
    monkeypatch.setattr(svc, "_listening_service_pids", lambda: [33024])

    calls: list[tuple[int, int]] = []

    def fake_stop_process_tree(pid: int, wait_seconds: int = 5) -> bool:
        calls.append((int(pid), int(wait_seconds)))
        alive[int(pid)] = False
        return True

    monkeypatch.setattr(svc, "stop_process_tree", fake_stop_process_tree)

    runner = CliRunner()
    result = runner.invoke(svc.webui, ["stop"])

    assert result.exit_code == 0
    assert calls == [(33024, 5)]
    assert db.get_service_state("webui", "_global") is None
    assert "Web UI 已停止" in result.output


def test_webui_stop_succeeds_if_process_exits_during_final_grace_period(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    db.upsert_service_state(
        "webui",
        "_global",
        pid=33024,
        status="running",
        log_path=str(svc.LOG_FILE),
        meta={"pid": 33024, "host": "127.0.0.1", "port": 8766},
    )

    alive = {33024: True}
    monkeypatch.setattr(svc, "is_process_alive", lambda pid: alive.get(int(pid), False))
    monkeypatch.setattr(svc, "_listening_service_pids", lambda: [33024] if alive[33024] else [])
    monkeypatch.setattr(svc, "stop_process_tree", lambda pid, wait_seconds=5: False)

    ticks = iter([0.0, 0.5, 1.0, 2.5])
    monkeypatch.setattr(svc.time, "monotonic", lambda: next(ticks))

    def fake_sleep(_seconds):
        alive[33024] = False

    monkeypatch.setattr(svc.time, "sleep", fake_sleep)

    runner = CliRunner()
    result = runner.invoke(svc.webui, ["stop"])

    assert result.exit_code == 0
    assert "以下 PID 停止时返回失败，但进程已退出：33024" in result.output
    assert "Web UI 已停止" in result.output
