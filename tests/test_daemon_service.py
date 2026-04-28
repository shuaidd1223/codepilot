from __future__ import annotations

import contextlib
import types

from click.testing import CliRunner

from codepilot.storage import database as db
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


def test_request_daemon_service_start_uses_external_launcher(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))

    calls = []

    def fake_spawn(cmd, *, log_file):
        calls.append((cmd, log_file))
        db.upsert_service_state(
            "daemon",
            "demo",
            pid=7654,
            status="running",
            log_path=str(log_file),
            meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
        )
        return 2468

    monkeypatch.setattr(daemon_cmd, "_spawn_detached_command_via_launcher", fake_spawn)
    monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: int(pid) == 7654)
    monkeypatch.setattr(daemon_cmd.time, "sleep", lambda _: None)

    result = daemon_cmd.request_daemon_service_start("demo", wait_seconds=1)

    assert result["running"] is True
    assert result["started"] is True
    assert result["pid"] == 7654
    assert result["launcher_pid"] == 2468
    assert calls
    cmd = calls[0][0]
    assert cmd[:4] == [daemon_cmd.sys.executable, "-m", "codepilot", "daemon"]
    assert cmd[-2:] == ["--project", "demo"]


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


def test_run_loop_wraps_foreground_backlog_drain_in_cli_progress_renderer(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))

    entered: list[tuple[str, bool]] = []

    @contextlib.contextmanager
    def _fake_renderer(*, enabled: bool = True):
        entered.append(("enter", enabled))
        yield
        entered.append(("exit", enabled))

    monkeypatch.setattr("codepilot.core.cli_progress.maybe_cli_renderer", _fake_renderer)
    monkeypatch.setattr(daemon_cmd, "_start_heartbeat_thread", lambda project: types.SimpleNamespace(set=lambda: None))
    monkeypatch.setattr(daemon_cmd, "_tick_heartbeat", lambda project: None)
    monkeypatch.setattr(daemon_cmd, "_stop_requested", lambda project: False)
    monkeypatch.setattr(daemon_cmd, "reap_stalled_tasks", lambda project: [])
    monkeypatch.setattr(daemon_cmd, "_get_combined_stats", lambda project: {"backlog": 1, "in_progress": 0})
    monkeypatch.setattr(daemon_cmd, "_sleep_or_stop", lambda project, interval: True)

    drained = []
    monkeypatch.setattr(
        daemon_cmd,
        "run_backlog",
        lambda *args, **kwargs: drained.append((args, kwargs))
        or {"processed": 1, "done": 1, "failed": 0, "requeued": 0},
    )

    daemon_cmd._run_loop("demo", interval=1, verbose=False, shell="auto", executor="builtin", auto_commit=False)

    assert drained
    assert entered == [("enter", True), ("exit", True)]

