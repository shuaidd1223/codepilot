from __future__ import annotations

from click.testing import CliRunner

from codepilot import db
from codepilot.commands import daemon as daemon_cmd


def _isolate_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "daemon"
    monkeypatch.setattr(daemon_cmd, "DAEMON_STATE_DIR", state_dir)
    return state_dir


def test_daemon_command_starts_detached_by_default(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))

    class _FakeProc:
        pid = 7654
        returncode = None

        def poll(self):
            return None

    calls = []
    monkeypatch.setattr(
        daemon_cmd,
        "_spawn_detached_daemon",
        lambda **kwargs: calls.append(kwargs) or _FakeProc(),
    )
    monkeypatch.setattr(daemon_cmd.time, "sleep", lambda _: None)

    result = CliRunner().invoke(daemon_cmd.daemon, ["--project", "demo"])

    assert result.exit_code == 0, result.output
    assert calls
    assert calls[0]["project"] == "demo"
    assert calls[0]["executor"] == "auto"
    assert "Daemon 已后台启动" in result.output
    state = db.get_service_state("daemon", "demo")
    assert state is not None
    assert state["pid"] == 7654


def test_daemon_status_reads_existing_service(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))
    db.upsert_service_state(
        "daemon",
        "demo",
        pid=7654,
        status="running",
        log_path="D:/tmp/daemon.log",
        meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
    )
    monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: int(pid) == 7654)

    result = CliRunner().invoke(daemon_cmd.daemon, ["--project", "demo", "--status"])

    assert result.exit_code == 0
    assert "Daemon 运行中" in result.output
    assert "PID=7654" in result.output


def test_daemon_stop_requests_graceful_polling_stop(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))
    db.upsert_service_state(
        "daemon",
        "demo",
        pid=7654,
        status="running",
        log_path="D:/tmp/daemon.log",
        meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
    )
    monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: int(pid) == 7654)

    result = CliRunner().invoke(daemon_cmd.daemon, ["--project", "demo", "--stop"])

    assert result.exit_code == 0, result.output
    assert "Daemon 已请求停止轮询" in result.output
    assert "不会被中断" in result.output
    state = db.get_service_state("daemon", "demo")
    assert state is not None
    assert state["status"] == "stopping"


def test_daemon_start_requires_project(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))

    result = CliRunner().invoke(daemon_cmd.daemon, [])

    assert result.exit_code != 0
    assert "启动 daemon 必须指定 --project" in result.output


def test_daemon_foreground_ui_starts_detached_webui_process(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))

    # Don't enter the long-running scheduler loop in this unit test.
    monkeypatch.setattr(daemon_cmd, "_run_loop", lambda *args, **kwargs: None)

    calls = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Result()

    monkeypatch.setattr(daemon_cmd.subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        daemon_cmd.daemon,
        ["--project", "demo", "--foreground", "--ui", "--ui-port", "9911"],
    )

    assert result.exit_code == 0, result.output
    assert calls, "expected daemon foreground mode to invoke webui service"
    cmd = calls[0][0]
    assert cmd[:5] == [daemon_cmd.sys.executable, "-m", "codepilot", "ui", "start"]
    assert "--no-daemon" in cmd
    assert "--no-open" in cmd
    assert cmd[-2:] == ["--port", "9911"]
