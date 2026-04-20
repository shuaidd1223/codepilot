from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot.commands import webui_service as svc


def _isolate_state(tmp_path, monkeypatch):
    state_dir = tmp_path / ".codepilot"
    monkeypatch.setattr(svc, "STATE_DIR", state_dir)
    monkeypatch.setattr(svc, "PID_FILE", state_dir / "webui.pid")
    monkeypatch.setattr(svc, "META_FILE", state_dir / "webui.json")
    monkeypatch.setattr(svc, "LOG_FILE", state_dir / "webui.log")
    return state_dir


def test_webui_stop_uses_runtime_process_tree_helper(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    svc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    svc.PID_FILE.write_text("4321", encoding="utf-8")
    svc.META_FILE.write_text(
        json.dumps({"pid": 4321, "host": "127.0.0.1", "port": 8766}),
        encoding="utf-8",
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
    assert not svc.PID_FILE.exists()
    assert not svc.META_FILE.exists()
    assert "Web UI 已停止" in result.output


def test_webui_restart_stops_existing_service_before_starting_new_one(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    svc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    svc.PID_FILE.write_text("4321", encoding="utf-8")
    svc.META_FILE.write_text(
        json.dumps({"pid": 4321, "host": "127.0.0.1", "port": 9988}),
        encoding="utf-8",
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
    result = runner.invoke(svc.webui, ["restart"])

    assert result.exit_code == 0
    assert calls == [(4321, 5)]
    assert svc.PID_FILE.read_text(encoding="utf-8").strip() == "5678"
    meta = json.loads(svc.META_FILE.read_text(encoding="utf-8"))
    assert meta["pid"] == 5678
    assert meta["host"] == "127.0.0.1"
    assert meta["port"] == 9988
    assert "Web UI 已启动" in result.output


def test_webui_stop_falls_back_to_listener_pid_when_pid_file_is_stale(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    svc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    svc.PID_FILE.write_text("61432", encoding="utf-8")
    svc.META_FILE.write_text(
        json.dumps({"pid": 61432, "host": "127.0.0.1", "port": 8766}),
        encoding="utf-8",
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
    assert not svc.PID_FILE.exists()
    assert not svc.META_FILE.exists()
    assert "Web UI 已停止" in result.output


def test_webui_stop_succeeds_if_process_exits_during_final_grace_period(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    svc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    svc.PID_FILE.write_text("33024", encoding="utf-8")
    svc.META_FILE.write_text(
        json.dumps({"pid": 33024, "host": "127.0.0.1", "port": 8766}),
        encoding="utf-8",
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
