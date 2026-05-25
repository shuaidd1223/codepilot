from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.core.memory import append_memory_event, read_memory_events
from codepilot.core.workflow_state import (
    create_agent_session,
    read_task_timeline_events,
    start_workflow,
    write_task_execution_artifacts,
)
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db as _init_test_db


def _register_demo_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "README.md").write_text("demo project\n", encoding="utf-8")
    return db.register_project("demo", str(project_path))


def test_workflow_supervisor_dry_run_reads_facts_and_suggests_retry(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    project_path = Path(project["path"])
    task = db.create_task("demo", "fix failed flow", content="## 任务目标\n\n修复失败流程", agent="codex")
    db.update_task(
        task["id"],
        status="failed",
        retry_count=1,
        max_retries=3,
        error_message="pytest failed in reviewer",
        completed_at="2026-05-25T10:10:00",
    )
    db.create_task_log(task["id"], "codex", "reviewer", output="FAIL: assertion", exit_code=1)
    artifact = write_task_execution_artifacts(
        project_path,
        task["id"],
        status="failed",
        source="run",
        executor="builtin",
        artifacts={
            "validation": {"kind": "validation", "status": "failed", "summary": "pytest failed"},
            "review": {"kind": "review", "status": "fail", "verdict": "fail", "summary": "review failed"},
        },
    )
    start_workflow(project_path, mode="plan", session_id="wf-supervisor", current_phase="executing")
    create_agent_session(project_path, goal="观察任务流")

    result = CliRunner().invoke(main, ["workflow", "supervisor", "-p", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    data = payload["data"]
    assert payload["command"] == "workflow supervisor"
    assert data["dry_run"] is True
    assert data["observed"]["tasks"]["by_status"]["failed"] == 1
    assert data["observed"]["workflow_state"]["mode"] == "plan"
    assert data["observed"]["agent_session"]["current_phase"] == "intake"
    assert data["observed"]["artifacts"][0]["artifact_path"] == artifact["artifact_path"]
    assert data["observed"]["trace"]["count"] > 0

    suggestion = data["suggestions"][0]
    assert suggestion["actor"] == "supervisor"
    assert suggestion["action"] == "retry_with_hint"
    assert suggestion["task_id"] == task["id"]
    assert suggestion["auto_executable"] is True
    assert suggestion["source_event"].startswith("task_timeline.")
    assert suggestion["artifact_path"] == artifact["artifact_path"]
    assert db.get_task(task["id"])["status"] == "failed"
    assert read_memory_events(project, event_type="supervisor.action_executed") == []


def test_workflow_supervisor_auto_executes_retry_with_audited_hint(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    project_path = Path(project["path"])
    task = db.create_task("demo", "retry me", content="## 任务目标\n\n修复失败", agent="codex")
    db.update_task(
        task["id"],
        status="failed",
        retry_count=1,
        max_retries=3,
        error_message="reviewer found a missing assertion",
        completed_at="2026-05-25T10:20:00",
    )
    artifact = write_task_execution_artifacts(
        project_path,
        task["id"],
        status="failed",
        source="run",
        executor="builtin",
        artifacts={"review": {"kind": "review", "status": "fail", "verdict": "fail", "summary": "missing assertion"}},
    )

    result = CliRunner().invoke(main, ["workflow", "supervisor", "-p", "demo", "--auto", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["executed"]["action"] == "retry_with_hint"
    assert data["executed"]["task_id"] == task["id"]

    updated = db.get_task(task["id"])
    assert updated["status"] == "backlog"
    assert "## Supervisor 重试提示" in updated["content"]
    assert "missing assertion" in updated["content"]

    events = read_memory_events(project, event_type="supervisor.action_executed")
    assert len(events) == 1
    details = events[0]["details"]
    assert details["actor"] == "supervisor"
    assert details["action"] == "retry_with_hint"
    assert details["task_id"] == task["id"]
    assert details["reason"]
    assert details["source_event"].startswith("task_timeline.")
    assert details["artifact_path"] == artifact["artifact_path"]

    timeline = read_task_timeline_events(project_path, task["id"])
    assert timeline[-1]["event"] == "supervised"
    assert "retry_with_hint" in timeline[-1]["message"]


def test_workflow_supervisor_downgrades_repeated_retry_to_clarification(tmp_path, monkeypatch):
    project = _register_demo_project(tmp_path, monkeypatch)
    project_path = Path(project["path"])
    task = db.create_task("demo", "looping failure", content="## 任务目标\n\n修复循环失败", agent="codex")
    db.update_task(
        task["id"],
        status="failed",
        retry_count=1,
        max_retries=3,
        error_message="same review failure",
        completed_at="2026-05-25T10:30:00",
    )
    write_task_execution_artifacts(
        project_path,
        task["id"],
        status="failed",
        source="run",
        executor="builtin",
        artifacts={"review": {"kind": "review", "status": "fail", "verdict": "fail", "summary": "same failure"}},
    )
    for index in range(2):
        append_memory_event(
            project,
            event_type="supervisor.action_executed",
            source="codepilot.supervisor",
            summary=f"Supervisor 执行 retry_with_hint：#{task['id']}",
            details={
                "actor": "supervisor",
                "action": "retry_with_hint",
                "task_id": task["id"],
                "reason": "same failure",
                "source_event": "task_timeline.reviewed",
                "artifact_path": str(project_path / ".codepilot" / "artifacts" / "tasks" / f"task-{task['id']}-execution.json"),
            },
            tags=["supervisor", "control"],
            timestamp=f"2026-05-25T10:3{index}:00Z",
        )

    result = CliRunner().invoke(main, ["workflow", "supervisor", "-p", "demo", "--loop-threshold", "2", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    suggestion = data["suggestions"][0]
    assert suggestion["action"] == "request_clarification"
    assert suggestion["downgraded_from"] == "retry_with_hint"
    assert data["loop_control"]["downgraded"][0]["task_id"] == task["id"]
    assert data["loop_control"]["downgraded"][0]["from_action"] == "retry_with_hint"
