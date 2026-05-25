from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest
from click.testing import CliRunner

from codepilot.storage import database as db
from codepilot.ai_support import service as ai_mod
from codepilot.core import progress_bus
from codepilot.core import runtime as runtime_mod
from codepilot.cli import main
from codepilot.commands import run as run_cmd
from tests.workflow_testkit import init_test_db as _init_test_db


def test_reap_stalled_tasks_marks_dead_in_progress_task_failed(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stuck task", agent="codex", max_retries=1)

    db.update_task(
        task["id"],
        status="in_progress",
        started_at="2026-01-01T00:00:00",
        heartbeat_at="2026-01-01T00:00:00",
        active_pid=None,
        run_phase="builder",
    )

    reaped = runtime_mod.reap_stalled_tasks("demo", stale_after_seconds=1)
    current = db.get_task(task["id"])

    assert len(reaped) == 1
    assert current["status"] == "failed"
    assert current["retry_count"] == 1
    assert "心跳已超过" in (current["error_message"] or "")


def test_reap_stalled_tasks_requeues_when_retries_remain(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stale but retryable", agent="codex", max_retries=3)

    db.update_task(
        task["id"],
        status="in_progress",
        started_at="2026-01-01T00:00:00",
        heartbeat_at="2026-01-01T00:00:00",
        active_pid=None,
        run_phase="builder",
    )

    reaped = runtime_mod.reap_stalled_tasks("demo", stale_after_seconds=1)
    current = db.get_task(task["id"])

    assert len(reaped) == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1
    assert "回退到 backlog" in (current["error_message"] or "")


def test_reap_stalled_tasks_cleans_dead_process_tree_before_marking_failed(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stale task", agent="codex", max_retries=1)

    db.update_task(
        task["id"],
        status="in_progress",
        started_at="2026-01-01T00:00:00",
        heartbeat_at="2026-01-01T00:00:00",
        active_pid=123456,
        run_phase="builder",
    )

    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: False)
    killed: list[int] = []
    monkeypatch.setattr(runtime_mod, "stop_process_tree", lambda pid, wait_seconds=5: killed.append(int(pid)) or True)

    reaped = runtime_mod.reap_stalled_tasks("demo", stale_after_seconds=1)
    current = db.get_task(task["id"])

    assert len(reaped) == 1
    assert killed == [123456]
    assert current["status"] == "failed"


def test_stop_process_tree_windows_kills_descendants_even_if_root_is_gone(monkeypatch):
    import subprocess

    monkeypatch.setattr(runtime_mod.platform, "system", lambda: "Windows")

    processes = {
        200: {"parent": 100, "name": "codex.exe"},
        201: {"parent": 200, "name": "node.exe"},
    }

    def _descendants(root_pid: int) -> set[int]:
        found = set()
        while True:
            added = {pid for pid, meta in processes.items() if meta["parent"] in ({root_pid} | found)}
            if added.issubset(found):
                break
            found |= added
        return found

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0].lower() == "powershell.exe":
            payload = [
                {"ProcessId": pid, "ParentProcessId": meta["parent"], "Name": meta["name"]}
                for pid, meta in sorted(processes.items())
            ]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[0].lower() == "taskkill":
            target = int(cmd[cmd.index("/PID") + 1])
            # Simulate "root is gone": taskkill returns failure and does not cascade
            if target not in processes:
                return subprocess.CompletedProcess(cmd, 128, "", "not found")
            if "/T" in cmd:
                targets = _descendants(target) | {target}
            else:
                targets = {target}
            for pid in targets:
                processes.pop(pid, None)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: int(pid) in processes if pid else False)

    tick = {"t": 0.0}

    def fake_monotonic():
        tick["t"] += 0.4
        return tick["t"]

    monkeypatch.setattr(runtime_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(runtime_mod.time, "sleep", lambda _: None)

    assert runtime_mod.stop_process_tree(100, wait_seconds=1) is True
    assert processes == {}
    assert any(cmd[:4] == ["taskkill", "/PID", "200", "/T"] for cmd in calls)
    assert any(cmd[:4] == ["taskkill", "/PID", "201", "/T"] for cmd in calls)


def test_windows_is_process_alive_uses_kernel32_without_powershell(monkeypatch):
    monkeypatch.setattr(runtime_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        runtime_mod.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("is_process_alive should not spawn powershell on Windows"),
    )

    handles: list[int] = []

    class FakeKernel32:
        def OpenProcess(self, access, inherit, pid):
            assert pid == 1234
            return 99

        def GetExitCodeProcess(self, handle, code_ptr):
            handles.append(handle)
            code_ptr._obj.value = 259
            return 1

        def CloseHandle(self, handle):
            handles.append(-handle)
            return 1

    monkeypatch.setattr(
        runtime_mod.ctypes,
        "windll",
        types.SimpleNamespace(kernel32=FakeKernel32()),
        raising=False,
    )

    assert runtime_mod.is_process_alive(1234) is True
    assert handles == [99, -99]


def test_stop_process_tree_windows_falls_back_to_stop_process_when_taskkill_denied(monkeypatch):
    import subprocess

    monkeypatch.setattr(runtime_mod.platform, "system", lambda: "Windows")

    alive = {61432: True}
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0].lower() == "taskkill":
            return subprocess.CompletedProcess(cmd, 1, "", "Access is denied.")
        if cmd[:2] == ["powershell.exe", "-Command"] and "Stop-Process -Id 61432 -Force" in cmd[2]:
            alive[61432] = False
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: alive.get(int(pid), False) if pid else False)

    runtime_mod._windows_kill_pid(61432)

    assert alive[61432] is False
    assert any(cmd[0].lower() == "taskkill" for cmd in calls)
    assert any(cmd[:2] == ["powershell.exe", "-Command"] for cmd in calls)


def test_run_command_live_cleans_process_tree_on_unexpected_exception(tmp_path, monkeypatch):
    import sys

    log_path = tmp_path / "live.log"
    monkeypatch.setattr(run_cmd, "update_task_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_cmd, "get_stop_request", lambda task_id: (_ for _ in ()).throw(RuntimeError("boom")))

    killed: list[int] = []

    def _tracking_stop(pid):
        killed.append(int(pid))
        return runtime_mod.stop_process_tree(pid)

    monkeypatch.setattr(run_cmd, "stop_process_tree", _tracking_stop)

    try:
        run_cmd._run_command_live(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            task_id=1,
            phase="builder",
            log_path=log_path,
            timeout=30,
        )
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected RuntimeError")

    assert killed
    assert not runtime_mod.is_process_alive(killed[0])


def test_run_command_live_emits_task_log_stream_with_offsets(tmp_path, monkeypatch):
    log_path = tmp_path / "live.log"
    monkeypatch.setattr(run_cmd, "update_task_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_cmd, "get_stop_request", lambda task_id: (False, ""))

    progress_bus.clear_subscribers_for_tests()
    events: list[dict] = []
    with progress_bus.subscription(events.append):
        exit_code, output = run_cmd._run_command_live(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time;"
                    "sys.stdout.write('abcdef');sys.stdout.flush();"
                    "time.sleep(0.05);"
                    "sys.stdout.write('ghi');sys.stdout.flush()"
                ),
            ],
            task_id=7,
            phase="builder",
            log_path=log_path,
            timeout=10,
        )

    assert exit_code == 0
    assert "abcdefghi" in output

    stream_events = [e for e in events if (e.get("extra") or {}).get("task_log_stream")]
    assert stream_events
    assert stream_events[0]["extra"]["task_log_start"] == 0
    for item in stream_events:
        extra = item["extra"]
        assert isinstance(extra.get("task_log_chunk"), str)
        assert int(extra.get("task_log_end") or 0) >= int(extra.get("task_log_start") or 0)

    combined = "".join((e.get("extra") or {}).get("task_log_chunk") or "" for e in stream_events)
    assert "abcdefghi" in combined


def test_run_command_live_writes_idle_heartbeat_when_subprocess_is_silent(tmp_path, monkeypatch):
    log_path = tmp_path / "silent.log"
    runtime_updates: list[dict] = []
    monkeypatch.setattr(run_cmd, "update_task_runtime", lambda *args, **kwargs: runtime_updates.append(kwargs))
    monkeypatch.setattr(run_cmd, "get_stop_request", lambda task_id: (False, ""))
    monkeypatch.setattr("codepilot.commands.run_live_runner.HEARTBEAT_INTERVAL_SECONDS", 0.1)

    progress_bus.clear_subscribers_for_tests()
    events: list[dict] = []
    with progress_bus.subscription(events.append):
        exit_code, output = run_cmd._run_command_live(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            task_id=17,
            phase="builder",
            log_path=log_path,
            timeout=10,
        )

    assert exit_code == 0

    body = log_path.read_text(encoding="utf-8", errors="replace")
    assert "子进程仍在运行" not in body
    assert "## Result" in body

    stream_chunks = [
        (event.get("extra") or {}).get("task_log_chunk") or ""
        for event in events
        if (event.get("extra") or {}).get("task_log_stream")
    ]
    assert not any("子进程仍在运行" in chunk for chunk in stream_chunks)
    run_status_events = [
        event for event in events
        if (event.get("extra") or {}).get("source") == "task_run_status"
    ]
    assert run_status_events
    assert any((event.get("extra") or {}).get("silent_seconds", 0) >= 0 for event in run_status_events)
    assert runtime_updates


def test_run_command_live_writes_timeout_status_to_log(tmp_path, monkeypatch):
    log_path = tmp_path / "timeout.log"
    monkeypatch.setattr(run_cmd, "update_task_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_cmd, "get_stop_request", lambda task_id: (False, ""))

    with pytest.raises(subprocess.TimeoutExpired):
        run_cmd._run_command_live(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            task_id=8,
            phase="builder",
            log_path=log_path,
            timeout=1,
        )

    body = log_path.read_text(encoding="utf-8", errors="replace")
    assert "## Result" in body
    assert "status: `timeout`" in body
    assert "超过超时阈值 1s" in body


def test_stop_command_cancels_in_progress_task_without_live_process(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stop me", agent="codex")
    db.update_task(task["id"], status="in_progress", active_pid=999999, run_phase="builder")

    monkeypatch.setattr("codepilot.commands.tasks.is_process_alive", lambda pid: False)
    monkeypatch.setattr("codepilot.commands.tasks.stop_process_tree", lambda pid: True)

    runner = CliRunner()
    result = runner.invoke(main, ["task", "stop", str(task["id"])])
    current = db.get_task(task["id"])

    assert result.exit_code == 0
    assert "已停止" in result.output
    assert current["status"] == "cancelled"


@pytest.mark.parametrize(
    ("args_template", "initial_status"),
    [
        (["task", "done", "{id}", "-m", "premature"], "in_progress"),
        (["task", "edit", "{id}", "--status", "done"], "backlog"),
        (["task", "retry", "{id}"], "failed"),
        (["task", "rm", "{id}", "--force"], "done"),
        (["task", "cancel", "{id}", "-m", "cancel"], "backlog"),
        (["task", "archive", "{id}"], "done"),
        (["task", "stop", "{id}", "-m", "stop"], "in_progress"),
    ],
)
def test_task_mutation_commands_reject_current_runner_task(
    tmp_path,
    monkeypatch,
    args_template,
    initial_status,
):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "owned by runner", agent="codex")
    db.update_task(
        task["id"],
        status=initial_status,
        active_pid=999999 if initial_status == "in_progress" else None,
        run_phase="builder" if initial_status == "in_progress" else None,
    )
    monkeypatch.setenv("CODEPILOT_RUNNER_TASK_ID", str(task["id"]))
    monkeypatch.setenv("CODEPILOT_RUNNER_PHASE", "builder")

    args = [str(task["id"]) if item == "{id}" else item for item in args_template]
    result = CliRunner().invoke(main, args)
    current = db.get_task(task["id"])

    assert result.exit_code != 0
    assert "runner 管理" in result.output
    assert current is not None
    assert current["status"] == initial_status


def test_task_mutation_guard_allows_other_task_and_plain_shell(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    current = db.create_task("demo", "current runner task", agent="codex")
    other = db.create_task("demo", "other task", agent="codex")
    monkeypatch.setenv("CODEPILOT_RUNNER_TASK_ID", str(current["id"]))
    monkeypatch.setenv("CODEPILOT_RUNNER_PHASE", "builder")

    runner = CliRunner()
    other_result = runner.invoke(main, ["task", "done", str(other["id"]), "-m", "ok"])

    assert other_result.exit_code == 0
    assert db.get_task(current["id"])["status"] == "backlog"
    assert db.get_task(other["id"])["status"] == "done"

    monkeypatch.delenv("CODEPILOT_RUNNER_TASK_ID", raising=False)
    monkeypatch.delenv("CODEPILOT_RUNNER_PHASE", raising=False)
    current_result = runner.invoke(main, ["task", "done", str(current["id"]), "-m", "manual"])

    assert current_result.exit_code == 0
    assert db.get_task(current["id"])["status"] == "done"


def test_cancel_command_rejects_in_progress_done_and_archived(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    backlog = db.create_task("demo", "cancel backlog", agent="codex")
    running = db.create_task("demo", "cancel running", agent="codex")
    done = db.create_task("demo", "cancel done", agent="codex")
    archived = db.create_task("demo", "cancel archived", agent="codex")

    db.update_task(running["id"], status="in_progress")
    db.update_task(done["id"], status="done")
    db.update_task(archived["id"], status="archived")

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "task",
            "cancel",
            str(backlog["id"]),
            str(running["id"]),
            str(done["id"]),
            str(archived["id"]),
            "-m",
            "manual cancel",
        ],
    )

    assert result.exit_code == 0
    assert "共取消 1 个任务" in result.output
    assert "不能取消；请使用 task stop" in result.output
    assert "已完成，无法取消" in result.output
    assert "已归档，无法取消" in result.output
    assert db.get_task(backlog["id"])["status"] == "cancelled"
    assert db.get_task(running["id"])["status"] == "in_progress"
    assert db.get_task(done["id"])["status"] == "done"
    assert db.get_task(archived["id"])["status"] == "archived"


def test_archive_command_only_allows_done_tasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    done = db.create_task("demo", "done archive", agent="codex")
    backlog = db.create_task("demo", "backlog archive", agent="codex")
    cancelled = db.create_task("demo", "cancelled archive", agent="codex")

    db.update_task(done["id"], status="done")
    db.update_task(cancelled["id"], status="cancelled")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["task", "archive", str(done["id"]), str(backlog["id"]), str(cancelled["id"])],
    )

    assert result.exit_code == 0
    assert "共归档 1 个任务" in result.output
    assert "只有已完成任务可以归档" in result.output
    assert db.get_task(done["id"])["status"] == "archived"
    assert db.get_task(backlog["id"])["status"] == "backlog"
    assert db.get_task(cancelled["id"])["status"] == "cancelled"


def test_rm_command_only_deletes_allowed_statuses(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    backlog = db.create_task("demo", "rm backlog", agent="codex")
    cancelled = db.create_task("demo", "rm cancelled", agent="codex")
    done = db.create_task("demo", "rm done", agent="codex")
    archived = db.create_task("demo", "rm archived", agent="codex")
    running = db.create_task("demo", "rm running", agent="codex")
    failed = db.create_task("demo", "rm failed", agent="codex")

    db.update_task(cancelled["id"], status="cancelled")
    db.update_task(done["id"], status="done")
    db.update_task(archived["id"], status="archived")
    db.update_task(running["id"], status="in_progress")
    db.update_task(failed["id"], status="failed")

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "task",
            "rm",
            str(backlog["id"]),
            str(cancelled["id"]),
            str(done["id"]),
            str(archived["id"]),
            str(running["id"]),
            str(failed["id"]),
            "--force",
        ],
    )

    assert result.exit_code == 0
    assert "已删除 4 个任务" in result.output
    assert "不能删除；请先停止" in result.output
    assert "不允许直接删除" in result.output
    assert db.get_task(backlog["id"]) is None
    assert db.get_task(cancelled["id"]) is None
    assert db.get_task(done["id"]) is None
    assert db.get_task(archived["id"]) is None
    assert db.get_task(running["id"])["status"] == "in_progress"
    assert db.get_task(failed["id"])["status"] == "failed"


def test_edit_and_find_support_archived_status(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "archived status", agent="codex")

    runner = CliRunner()
    edit_result = runner.invoke(main, ["task", "edit", str(task["id"]), "--status", "archived"])
    find_result = runner.invoke(main, ["task", "find", "--status", "archived", "--json"])

    assert edit_result.exit_code == 0
    assert db.get_task(task["id"])["status"] == "archived"
    assert find_result.exit_code == 0
    payload = json.loads(find_result.output)
    assert payload["ok"] is True
    assert payload["data"]["count"] == 1
    assert payload["data"]["tasks"][0]["id"] == task["id"]


def test_logs_command_reads_live_runtime_log(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "show logs", agent="codex")
    log_path = tmp_path / "task.log"
    log_path.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    db.update_task(task["id"], status="in_progress", current_log_path=str(log_path))

    runner = CliRunner()
    result = runner.invoke(main, ["task", "logs", str(task["id"]), "--tail", "2"])

    assert result.exit_code == 0
    assert "line 2" in result.output
    assert "line 3" in result.output


def test_show_command_prints_full_task_detail(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    content = "第一行\n第二行 precise detail should not be truncated"
    task = db.create_task("demo", "inspect exact task", content=content, agent="codex", depends_on=[1, 2])
    db.update_task(
        task["id"],
        status="failed",
        error_message="full error message",
        delivery_record="delivery notes",
        last_output="last output line",
    )
    db.create_task_log(task["id"], "codex", "builder", output="log body", exit_code=1, duration=12)

    runner = CliRunner()
    result = runner.invoke(main, ["task", "show", str(task["id"])])

    assert result.exit_code == 0
    assert "任务详情" in result.output
    assert "inspect exact task" in result.output
    assert "depends_on: #1, #2" in result.output
    assert content in result.output
    assert "full error message" in result.output
    assert "delivery notes" in result.output
    assert "last output line" in result.output
    assert "执行日志" in result.output
    assert "完整日志: codepilot task logs" in result.output
    assert "log body" not in result.output


def test_show_command_explains_empty_task_content(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "empty content task", content="", agent="codex")

    runner = CliRunner()
    result = runner.invoke(main, ["task", "show", str(task["id"])])

    assert result.exit_code == 0
    assert "任务内容" in result.output
    assert "空正文" in result.output
    assert "ai template --format json" in result.output


def test_show_json_outputs_full_task_and_logs(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "json detail", content="json content", agent="codex", depends_on=[9])
    db.create_task_log(task["id"], "codex", "builder", output="full log output", exit_code=0)

    runner = CliRunner()
    result = runner.invoke(main, ["task", "show", str(task["id"]), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "show"
    assert payload["data"]["task"]["id"] == task["id"]
    assert payload["data"]["task"]["title"] == "json detail"
    assert payload["data"]["task"]["content"] == "json content"
    assert payload["data"]["task"]["depends_on_ids"] == [9]
    assert payload["data"]["logs"][0]["output"] == "full log output"


def test_show_global_json_outputs_full_task_payload(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "global json detail", content="global json content", agent="codex")

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "task", "show", str(task["id"])])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "show"
    assert payload["data"]["task"]["id"] == task["id"]
    assert payload["data"]["task"]["title"] == "global json detail"
    assert payload["data"]["task"]["content"] == "global json content"
    assert payload["data"]["logs"] == []


def test_show_logs_flag_prints_full_log_output(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "logs detail", agent="codex")
    db.create_task_log(task["id"], "codex", "builder", output="full\nlog\nbody", exit_code=None, duration=None)

    runner = CliRunner()
    result = runner.invoke(main, ["task", "show", str(task["id"]), "--logs"])

    assert result.exit_code == 0
    assert "执行日志" in result.output
    # New layout renders log entries as a Rich table with 退出/耗时 columns
    # plus a per-entry separator before the raw output dump.
    assert "退出" in result.output
    assert "耗时" in result.output
    assert "full\nlog\nbody" in result.output
    assert "完整日志: codepilot task logs" not in result.output


def test_show_handles_malformed_depends_on_without_crashing(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "bad depends", agent="codex")
    with db.get_conn() as conn:
        conn.execute("UPDATE tasks SET depends_on = ? WHERE id = ?", ("not-json [", task["id"]))
        conn.commit()
    db._cache_invalidate("task_by_id")

    runner = CliRunner()
    result = runner.invoke(main, ["task", "show", str(task["id"])])

    assert result.exit_code == 0
    assert "bad depends" in result.output
    assert "depends_on: -" in result.output
    assert "builder: -" in result.output
    assert "active_pid: -" in result.output
    assert "Traceback" not in result.output


def test_show_json_depends_on_ids_ignore_invalid_entries(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "mixed depends", agent="codex")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET depends_on = ? WHERE id = ?",
            (json.dumps([1, "2", "bad", None, {}, []]), task["id"]),
        )
        conn.commit()
    db._cache_invalidate("task_by_id")

    runner = CliRunner()
    result = runner.invoke(main, ["task", "show", str(task["id"]), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "show"
    assert payload["data"]["task"]["depends_on"] == '[1, "2", "bad", null, {}, []]'
    assert payload["data"]["task"]["depends_on_ids"] == [1, 2]


def test_show_missing_task_exits_nonzero_in_text_and_json(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)

    runner = CliRunner()
    text_result = runner.invoke(main, ["task", "show", "99999"])

    assert text_result.exit_code != 0
    assert "任务 #99999 不存在" in text_result.output

    for args in (["task", "show", "99999", "--json"], ["--json", "task", "show", "99999"]):
        json_result = runner.invoke(main, args)

        assert json_result.exit_code != 0
        payload = json.loads(json_result.output)
        assert payload["ok"] is False
        assert payload["command"] == "show"
        assert payload["data"] == {"task": None, "logs": []}
        assert payload["error"]["code"] == "task_not_found"


def test_status_verbose_shows_runtime_summary_for_in_progress_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "visible task", agent="codex")
    db.update_task(
        task["id"],
        status="in_progress",
        run_phase="builder",
        heartbeat_at="2999-01-01T00:00:00",
        active_pid=None,
        last_output="running tests",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["status", "-p", "demo", "-v"])

    assert result.exit_code == 0
    assert "builder" in result.output
    assert "running tests" in result.output


def test_status_json_returns_contract_for_single_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    db.create_task("demo", "json status task", agent="codex")

    runner = CliRunner()
    result = runner.invoke(main, ["status", "-p", "demo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "status"
    assert payload["data"]["project"] == "demo"
    assert payload["data"]["stats"]["total"] == 1
    assert payload["data"]["tasks"][0]["title"] == "json status task"


def test_status_global_json_wraps_projects_list_in_contract(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    db.register_project("demo-a", str(project_a))
    db.register_project("demo-b", str(project_b))
    db.create_task("demo-a", "task a", agent="codex")
    db.create_task("demo-b", "task b", agent="codex")

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "status"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "status"
    assert payload["data"]["count"] == 2
    names = {item["project"] for item in payload["data"]["projects"]}
    assert names == {"demo-a", "demo-b"}


def test_extract_error_hint_humanizes_json_payload():
    raw = """{"type":"result","subtype":"success","is_error":true,"result":"You've hit your limit · resets Apr 14, 1pm (Asia/Shanghai)"}"""

    hint = ai_mod._extract_error_hint(raw)

    assert "当前账号额度已用完" in hint
    assert "重置时间 Apr 14, 1pm" in hint
    assert "{" not in hint
