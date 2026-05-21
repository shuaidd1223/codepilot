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
    state_dir = _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))

    class _FakeProc:
        pid = 7654
        returncode = None

        def poll(self):
            return None

    calls = []

    def fake_spawn(**kwargs):
        calls.append(kwargs)
        db.upsert_service_state(
            "daemon",
            "demo",
            pid=7654,
            status="running",
            log_path=str(state_dir / "demo" / "daemon.log"),
            meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
        )
        return _FakeProc()

    monkeypatch.setattr(
        daemon_cmd,
        "_spawn_detached_daemon",
        fake_spawn,
    )
    monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: int(pid) == 7654)
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


def test_start_daemon_service_waits_for_foreground_child_state(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))

    class _FakeProc:
        pid = 7654
        returncode = None

        def poll(self):
            return None

    statuses = [
        {"running": False, "pid": 0, "project": "", "started_at": "", "log": str(tmp_path / "daemon.log")},
        {"running": False, "pid": 0, "project": "", "started_at": "", "log": str(tmp_path / "daemon.log")},
        {
            "running": True,
            "pid": 8765,
            "project": "demo",
            "started_at": "2026-01-01T00:00:00",
            "log": str(tmp_path / "daemon.log"),
        },
    ]

    def fake_status(project):
        return statuses.pop(0) if statuses else {
            "running": True,
            "pid": 8765,
            "project": project or "",
            "started_at": "2026-01-01T00:00:00",
            "log": str(tmp_path / "daemon.log"),
        }

    monkeypatch.setattr(daemon_cmd, "_spawn_detached_daemon", lambda **kwargs: _FakeProc())
    monkeypatch.setattr(daemon_cmd, "daemon_service_status", fake_status)
    monkeypatch.setattr(daemon_cmd, "_write_meta", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("parent must not write daemon state")))
    monkeypatch.setattr(daemon_cmd.time, "sleep", lambda _: None)

    result = daemon_cmd.start_daemon_service(project="demo")

    assert result["running"] is True
    assert result["started"] is True
    assert result["pid"] == 8765


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


def test_daemon_status_clears_stale_heartbeat_even_when_pid_exists(tmp_path, monkeypatch):
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
        heartbeat_at="2026-01-01T00:00:00",
        meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
    )
    monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: int(pid) == 7654)

    status = daemon_cmd.daemon_service_status("demo", stale_after_seconds=120)

    assert status["running"] is False
    assert status["pid"] == 0
    assert status["stale"] is True
    assert db.get_service_state("daemon", "demo") is None


def test_daemon_foreground_lock_allows_parent_published_same_pid(tmp_path, monkeypatch):
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
    monkeypatch.setattr(daemon_cmd.os, "getpid", lambda: 7654)
    monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: int(pid) == 7654)

    assert daemon_cmd._acquire_lock("demo") is True

    state = db.get_service_state("daemon", "demo")
    assert state is not None
    assert state["pid"] == 7654
    assert state["meta"]["executor"] == "foreground"


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


def test_daemon_stop_requested_refreshes_service_state_cache(tmp_path, monkeypatch):
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
    assert db.get_service_state("daemon", "demo")["status"] == "running"
    with db.get_write_conn() as conn:
        conn.execute(
            "UPDATE service_states SET status = 'stopping' WHERE service = 'daemon' AND scope = 'demo'"
        )

    assert daemon_cmd._stop_requested("demo") is True


def test_daemon_heartbeat_preserves_external_stop_request(tmp_path, monkeypatch):
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
    assert db.get_service_state("daemon", "demo")["status"] == "running"
    with db.get_write_conn() as conn:
        conn.execute(
            "UPDATE service_states SET status = 'stopping' WHERE service = 'daemon' AND scope = 'demo'"
        )

    daemon_cmd._tick_heartbeat("demo")

    assert db.get_service_state("daemon", "demo")["status"] == "stopping"


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


def test_request_daemon_service_start_uses_binary_command_when_frozen(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))
    monkeypatch.setattr(daemon_cmd.sys, "frozen", True, raising=False)
    monkeypatch.setattr(daemon_cmd.sys, "executable", r"C:\Tools\CodePilot\codepilot.exe")

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
    assert calls[0][0] == [r"C:\Tools\CodePilot\codepilot.exe", "daemon", "--project", "demo"]


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


def test_daemon_foreground_ui_uses_binary_command_when_frozen(tmp_path, monkeypatch):
    _isolate_state(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_cmd.sys, "frozen", True, raising=False)
    monkeypatch.setattr(daemon_cmd.sys, "executable", r"C:\Tools\CodePilot\codepilot.exe")
    calls = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(daemon_cmd.subprocess, "run", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or _Result())

    assert daemon_cmd._ensure_ui_service_process(9911) is True
    cmd = calls[0][0]
    assert cmd[:3] == [r"C:\Tools\CodePilot\codepilot.exe", "ui", "start"]
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
