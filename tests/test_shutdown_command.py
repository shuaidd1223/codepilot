from __future__ import annotations

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands import shutdown as shutdown_cmd
from codepilot.storage import database as db


def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path / "demo"))


def test_shutdown_registered_as_top_level_command(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["shutdown", "--help"])

    assert result.exit_code == 0, result.output
    assert "shutdown" in result.output


def test_shutdown_stops_webui_and_feishu_before_waiting(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    events: list[str] = []
    active = shutdown_cmd.ActiveWork(
        daemons=[shutdown_cmd.ServiceTarget(service="daemon", scope="demo", pid=1111)],
        inspections=[],
        tasks=[],
    )

    monkeypatch.setattr(shutdown_cmd, "_stop_webui_service", lambda: events.append("webui"))
    monkeypatch.setattr(shutdown_cmd, "_stop_feishu_service", lambda: events.append("feishu"))
    monkeypatch.setattr(shutdown_cmd, "_stop_webhook_service", lambda: events.append("webhook"))
    monkeypatch.setattr(shutdown_cmd, "_collect_active_work", lambda project=None: active)
    monkeypatch.setattr(shutdown_cmd, "_request_graceful_stop", lambda work: events.append("graceful"))
    monkeypatch.setattr(shutdown_cmd, "_wait_for_idle", lambda project=None, poll_interval=2.0: events.append("wait"))

    result = CliRunner().invoke(shutdown_cmd.shutdown, input="n\n")

    assert result.exit_code == 0, result.output
    assert events == ["webui", "feishu", "webhook", "graceful", "wait"]
    assert "waiting for active work" in result.output


def test_shutdown_y_force_stops_known_pids_and_cancels_tasks(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    task = db.create_task("demo", "running task", agent="codex")
    db.update_task(task["id"], status="in_progress", active_pid=3333, run_phase="builder")
    db.upsert_service_state("daemon", "demo", pid=1111, status="running", log_path="daemon.log")
    db.upsert_service_state("inspect", "demo", pid=2222, status="running", log_path="inspect.log")

    alive = {1111, 2222, 3333}
    stopped: list[int] = []

    def _stop(pid: int, wait_seconds: int = 5) -> bool:
        stopped.append(int(pid))
        alive.discard(int(pid))
        return True

    monkeypatch.setattr(shutdown_cmd, "_stop_webui_service", lambda: None)
    monkeypatch.setattr(shutdown_cmd, "_stop_feishu_service", lambda: None)
    monkeypatch.setattr(shutdown_cmd, "_stop_webhook_service", lambda: None)
    monkeypatch.setattr(shutdown_cmd, "is_process_alive", lambda pid: int(pid or 0) in alive)
    monkeypatch.setattr(shutdown_cmd, "stop_process_tree", _stop)
    monkeypatch.setattr(shutdown_cmd, "stop_worktree_leftovers", lambda *args, **kwargs: [])

    result = CliRunner().invoke(shutdown_cmd.shutdown, input="Y\n")

    assert result.exit_code == 0, result.output
    assert stopped == [1111, 2222, 3333]
    assert db.get_service_state("daemon", "demo") is None
    assert db.get_service_state("inspect", "demo") is None
    updated = db.get_task(task["id"])
    assert updated["status"] == "cancelled"
    assert updated["active_pid"] is None
    assert "force-stopped by codepilot shutdown" in updated["error_message"]


def test_shutdown_wait_mode_does_not_cancel_running_tasks(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    task = db.create_task("demo", "keep running", agent="codex")
    db.update_task(task["id"], status="in_progress", active_pid=3333, run_phase="builder")

    events: list[str] = []
    monkeypatch.setattr(shutdown_cmd, "_stop_webui_service", lambda: events.append("webui"))
    monkeypatch.setattr(shutdown_cmd, "_stop_feishu_service", lambda: events.append("feishu"))
    monkeypatch.setattr(shutdown_cmd, "_stop_webhook_service", lambda: events.append("webhook"))
    monkeypatch.setattr(shutdown_cmd, "_request_graceful_stop", lambda work: events.append(f"graceful:{len(work.tasks)}"))
    monkeypatch.setattr(shutdown_cmd, "_wait_for_idle", lambda project=None, poll_interval=2.0: events.append("wait"))
    monkeypatch.setattr(
        shutdown_cmd,
        "stop_process_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("wait mode must not kill task PIDs")),
    )

    result = CliRunner().invoke(shutdown_cmd.shutdown, input="anything-but-Y\n")

    assert result.exit_code == 0, result.output
    assert events == ["webui", "feishu", "webhook", "graceful:1", "wait"]
    current = db.get_task(task["id"])
    assert current["status"] == "in_progress"
    assert current["active_pid"] == 3333


def test_shutdown_stops_webhook_processes_with_codepilot_webhook_command(monkeypatch):
    alive = {1234, 5678}
    stopped: list[int] = []
    monkeypatch.setattr(shutdown_cmd.os, "getpid", lambda: 5678)
    monkeypatch.setattr(
        shutdown_cmd,
        "_webhook_processes",
        lambda: [
            {"pid": 1234, "command_line": "python -m codepilot webhook --port 8765"},
            {"pid": 5678, "command_line": "python -m codepilot shutdown"},
            {"pid": 9999, "command_line": "python other.py"},
        ],
    )
    monkeypatch.setattr(shutdown_cmd, "is_process_alive", lambda pid: int(pid or 0) in alive)

    def _stop(pid: int, wait_seconds: int = 5) -> bool:
        stopped.append(int(pid))
        alive.discard(int(pid))
        return True

    monkeypatch.setattr(shutdown_cmd, "stop_process_tree", _stop)

    result = shutdown_cmd._stop_webhook_service()

    assert result == {"stopped": True, "pids": [1234]}
    assert stopped == [1234]
