from __future__ import annotations

import ast
import inspect
import json

from click.testing import CliRunner

import codepilot.commands.trace as trace_cmd
from codepilot.cli import main
from codepilot.commands.trace import collect_trace_events
from codepilot.core.workflow_state import start_workflow
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _seed_trace(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = db.register_project("demo", str(project_path))

    task = db.create_task("demo", "实现 trace", "content", agent="codex", priority="P1")
    with db.get_write_conn() as conn:
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", ("2026-04-29 09:59:00", task["id"]))
    db.update_task(
        task["id"],
        status="in_progress",
        started_at="2026-04-29 10:00:00",
        heartbeat_at="2026-04-29 10:10:00",
        run_phase="builder",
        last_output="builder still running",
    )
    db.create_task_log(
        task["id"],
        "codex",
        "builder",
        output="pytest running",
        exit_code=0,
        started_at="2026-04-29 10:01:00",
        finished_at="2026-04-29 10:05:00",
        duration=240,
    )
    db.upsert_service_state(
        "webui",
        "demo",
        pid=123,
        status="running",
        heartbeat_at="2026-04-29 10:12:00",
        log_path=str(tmp_path / "webui.log"),
    )
    start_workflow(
        project_path,
        mode="plan",
        session_id="trace-test",
        current_phase="draft",
        started_at="2026-04-29 10:11:00",
    )
    return project, task


def test_collect_trace_events_merges_sources_in_recent_order(tmp_path, monkeypatch):
    project, task = _seed_trace(tmp_path, monkeypatch)

    events = collect_trace_events(project, limit=5)

    assert [event["source"] for event in events[:3]] == ["service", "workflow", "task"]
    assert events[0]["event"] == "service.heartbeat"
    assert events[1]["event"] == "workflow.updated"
    assert events[2]["event"] == "task.heartbeat"
    assert events[2]["task_id"] == task["id"]


def test_trace_command_outputs_json(tmp_path, monkeypatch):
    _seed_trace(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["trace", "-p", "demo", "--limit", "4", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "trace"
    assert payload["data"]["project"] == "demo"
    assert payload["data"]["count"] == 4
    assert payload["data"]["events"][0]["event"] == "service.heartbeat"


def test_trace_task_filter_excludes_service_and_workflow_events(tmp_path, monkeypatch):
    project, task = _seed_trace(tmp_path, monkeypatch)

    events = collect_trace_events(project, task_id=task["id"], limit=0)

    assert events
    assert {event["source"] for event in events} == {"task", "task_log"}
    assert all(event["task_id"] == task["id"] for event in events)


def test_collect_trace_events_respects_optional_project_sources(tmp_path, monkeypatch):
    project, _task = _seed_trace(tmp_path, monkeypatch)

    events = collect_trace_events(project, limit=0, include_services=False, include_workflow=False)

    assert events
    assert {event["source"] for event in events} == {"task", "task_log"}


def test_collect_trace_events_delegates_collection_branches():
    tree = ast.parse(inspect.getsource(trace_cmd.collect_trace_events))
    branch_nodes = (
        ast.If,
        ast.For,
        ast.While,
        ast.Try,
        ast.IfExp,
        ast.BoolOp,
        ast.comprehension,
    )

    assert sum(isinstance(node, branch_nodes) for node in ast.walk(tree)) <= 6
