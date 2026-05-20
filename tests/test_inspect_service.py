from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot.storage import database as db
from codepilot.commands import inspect as inspect_cmd


def _isolate_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "inspect"
    monkeypatch.setattr(inspect_cmd, "INSPECT_STATE_DIR", state_dir)
    return state_dir


def test_inspect_command_starts_detached_by_default(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.init_db()
    db.register_project("demo", str(project_path))

    class _FakeProc:
        pid = 8765
        returncode = None

        def poll(self):
            return None

    calls = []
    monkeypatch.setattr(
        inspect_cmd,
        "_spawn_detached_inspect",
        lambda *args, **kwargs: calls.append((args, kwargs)) or _FakeProc(),
    )
    monkeypatch.setattr(inspect_cmd.time, "sleep", lambda _: None)

    result = CliRunner().invoke(inspect_cmd.inspect, ["--project", "demo"])

    assert result.exit_code == 0, result.output
    assert calls
    assert calls[0][0][0] == "demo"
    assert "项目 demo 巡检已后台启动" in result.output
    state = db.get_service_state("inspect", "demo")
    assert state is not None
    assert state["pid"] == 8765


def test_request_inspect_service_start_uses_external_launcher(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.init_db()
    db.register_project("demo", str(project_path))

    calls = []

    def fake_spawn(cmd, *, log_file):
        calls.append((cmd, log_file))
        db.upsert_service_state(
            "inspect",
            "demo",
            pid=8765,
            status="running",
            log_path=str(log_file),
            meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
        )
        return 2468

    monkeypatch.setattr(inspect_cmd, "_spawn_detached_command_via_launcher", fake_spawn)
    monkeypatch.setattr(inspect_cmd, "is_process_alive", lambda pid: int(pid) == 8765)
    monkeypatch.setattr(inspect_cmd.time, "sleep", lambda _: None)

    result = inspect_cmd.request_inspect_service_start("demo", wait_seconds=1)

    assert result["running"] is True
    assert result["started"] is True
    assert result["pid"] == 8765
    assert result["launcher_pid"] == 2468
    assert calls
    cmd = calls[0][0]
    assert cmd[:4] == [inspect_cmd.sys.executable, "-m", "codepilot", "inspect"]
    assert cmd[-2:] == ["--project", "demo"]


def test_request_inspect_service_start_uses_binary_command_when_frozen(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.init_db()
    db.register_project("demo", str(project_path))
    monkeypatch.setattr(inspect_cmd.sys, "frozen", True, raising=False)
    monkeypatch.setattr(inspect_cmd.sys, "executable", r"C:\Tools\CodePilot\codepilot.exe")

    calls = []

    def fake_spawn(cmd, *, log_file):
        calls.append((cmd, log_file))
        db.upsert_service_state(
            "inspect",
            "demo",
            pid=8765,
            status="running",
            log_path=str(log_file),
            meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
        )
        return 2468

    monkeypatch.setattr(inspect_cmd, "_spawn_detached_command_via_launcher", fake_spawn)
    monkeypatch.setattr(inspect_cmd, "is_process_alive", lambda pid: int(pid) == 8765)
    monkeypatch.setattr(inspect_cmd.time, "sleep", lambda _: None)

    result = inspect_cmd.request_inspect_service_start("demo", wait_seconds=1)

    assert result["running"] is True
    assert calls[0][0] == [r"C:\Tools\CodePilot\codepilot.exe", "inspect", "--project", "demo"]


def test_inspect_status_reads_existing_service(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.init_db()
    db.register_project("demo", str(project_path))
    db.upsert_service_state(
        "inspect",
        "demo",
        pid=8765,
        status="running",
        log_path="D:/tmp/inspect.log",
        meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
    )
    monkeypatch.setattr(inspect_cmd, "is_process_alive", lambda pid: int(pid) == 8765)

    result = CliRunner().invoke(inspect_cmd.inspect, ["--project", "demo", "--status"])

    assert result.exit_code == 0
    assert "巡检运行中" in result.output
    assert "PID=8765" in result.output


def test_inspect_status_json_uses_command_contract(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.init_db()
    db.register_project("demo", str(project_path))
    monkeypatch.setattr(inspect_cmd, "is_process_alive", lambda _: False)

    result = CliRunner().invoke(inspect_cmd.inspect, ["--project", "demo", "--status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "inspect"
    assert payload["data"]["action"] == "status"
    assert payload["data"]["service"]["project"] == "demo"


def test_inspect_stop_json_reports_not_running_with_error_contract(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.init_db()
    db.register_project("demo", str(project_path))

    result = CliRunner().invoke(inspect_cmd.inspect, ["--project", "demo", "--stop", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["command"] == "inspect"
    assert payload["error"]["code"] == "service_not_running"
