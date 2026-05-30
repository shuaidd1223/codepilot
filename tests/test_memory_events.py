from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codepilot.ai_support.agent_support import command_manifest
from codepilot.cli import main
from codepilot.core.memory import (
    MemoryError,
    append_memory_event,
    memory_autocapture_path,
    memory_candidates_path,
    memory_events_path,
    read_memory_candidates,
    read_memory_events,
)
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _register_demo(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    return db.register_project("demo", str(project_path))


def test_memory_event_log_appends_project_local_fact_events(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)

    event = append_memory_event(
        project,
        event_type="inspect.workflow_context_written",
        source="codepilot.inspect",
        summary="巡检写入 workflow context",
        details={"context_path": ".codepilot/context/inspect-test.json", "created_count": 1},
        tags=["inspect", "workflow"],
    )

    path = memory_events_path(project)
    assert path == Path(project["path"]) / ".codepilot" / "memory" / "events.jsonl"
    assert path.is_file()
    assert event["event_id"].startswith("mem_")
    assert event["confidence"] == "fact"

    events = read_memory_events(project)
    assert len(events) == 1
    assert events[0]["event_type"] == "inspect.workflow_context_written"
    assert events[0]["details"]["created_count"] == 1


def test_memory_event_auto_captures_candidate_and_durable_summary(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)

    event = append_memory_event(
        project,
        event_type="workflow.action_executed",
        source="codepilot.workflow",
        summary="执行 workflow action：create_inspect_tasks",
        details={"action_id": "create_inspect_tasks", "result_keys": ["created_count"]},
        tags=["workflow", "action"],
    )

    candidates = read_memory_candidates(project)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["source_event_id"] == event["event_id"]
    assert candidate["candidate_id"].startswith("memcand_")
    assert candidate["status"] == "auto_promoted"
    assert candidate["target"] == "memory.autocapture"

    text = memory_autocapture_path(project).read_text(encoding="utf-8")
    assert "执行 workflow action：create_inspect_tasks" in text
    assert candidate["candidate_id"] in text


def test_memory_auto_capture_deduplicates_repeated_facts(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    kwargs = {
        "event_type": "workflow.action_executed",
        "source": "codepilot.workflow",
        "summary": "执行 workflow action：create_inspect_tasks",
        "details": {"action_id": "create_inspect_tasks"},
        "tags": ["workflow"],
    }

    append_memory_event(project, **kwargs)
    append_memory_event(project, **kwargs)

    candidates = read_memory_candidates(project)
    assert len(candidates) == 1
    text = memory_autocapture_path(project).read_text(encoding="utf-8")
    assert text.count("执行 workflow action：create_inspect_tasks") == 1


def test_memory_candidate_scores_workflow_action_feedback(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)

    append_memory_event(
        project,
        event_type="workflow.action_executed",
        source="codepilot.workflow",
        summary="执行 workflow action：promote_inspect_report_abc",
        details={"action_id": "promote_inspect_report_abc", "result_keys": ["task"]},
        tags=["workflow", "action"],
    )

    candidates = read_memory_candidates(project)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["feedback"] == "positive"
    assert candidate["score"] >= 85
    assert "inspect_report_promoted" in candidate["signals"]

    text = memory_autocapture_path(project).read_text(encoding="utf-8")
    assert "score=" in text
    assert "feedback=positive" in text


def test_memory_candidate_updates_seen_count_for_repeated_feedback(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    kwargs = {
        "event_type": "workflow.action_executed",
        "source": "codepilot.workflow",
        "summary": "执行 workflow action：create_inspect_tasks",
        "details": {"action_id": "create_inspect_tasks"},
        "tags": ["workflow"],
    }

    first = append_memory_event(project, timestamp="2026-05-22T00:00:00Z", **kwargs)
    second = append_memory_event(project, timestamp="2026-05-22T00:00:01Z", **kwargs)

    candidates = read_memory_candidates(project)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["seen_count"] == 2
    assert candidate["source_event_ids"] == [first["event_id"], second["event_id"]]
    assert candidate["last_seen_at"] == "2026-05-22T00:00:01Z"
    assert candidate["score"] >= 80


def test_memory_auto_capture_ignores_volatile_paths_in_candidate_identity(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)

    append_memory_event(
        project,
        event_type="workflow.action_executed",
        source="codepilot.workflow",
        summary="执行 workflow action：plan_from_inspect",
        details={"action_id": "plan_from_inspect", "source": {"context_path": "C:/tmp/a.json"}},
        tags=["workflow"],
    )
    append_memory_event(
        project,
        event_type="workflow.action_executed",
        source="codepilot.workflow",
        summary="执行 workflow action：plan_from_inspect",
        details={"action_id": "plan_from_inspect", "source": {"context_path": "D:/other/b.json"}},
        tags=["workflow"],
    )

    assert len(read_memory_candidates(project)) == 1


def test_memory_events_cli_lists_recent_events_and_filters_type(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    append_memory_event(project, event_type="workflow.action_executed", source="test", summary="执行动作")
    append_memory_event(project, event_type="inspect.workflow_context_written", source="test", summary="巡检上下文")

    result = CliRunner().invoke(
        main,
        ["memory", "events", "-p", "demo", "--type", "inspect.workflow_context_written", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["count"] == 1
    assert payload["data"]["events"][0]["summary"] == "巡检上下文"


def test_memory_event_rejects_secret_like_content(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)

    with pytest.raises(MemoryError, match="secret"):
        append_memory_event(project, event_type="memory.test", source="test", summary="FEISHU_APP_SECRET=abc123")
    assert not memory_candidates_path(project).exists()


def test_terminal_done_task_update_creates_positive_memory_feedback(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    task = db.create_task(project["name"], "完成巡检建议", content="do it", source="inspect")

    db.update_task(
        task["id"],
        status="done",
        completed_at="2026-05-22T01:00:00",
        delivery_record="验证通过",
    )

    events = read_memory_events(project, event_type="task.updated")
    assert len(events) == 1
    assert events[0]["details"]["task_id"] == task["id"]
    assert events[0]["details"]["status"] == "done"

    candidate = read_memory_candidates(project)[0]
    assert candidate["event_type"] == "task.updated"
    assert candidate["feedback"] == "positive"
    assert candidate["score"] >= 85
    assert "task_done" in candidate["signals"]


def test_terminal_failed_task_update_creates_negative_memory_feedback(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    task = db.create_task(project["name"], "失败巡检建议", content="do it", source="inspect")

    db.update_task(
        task["id"],
        status="failed",
        completed_at="2026-05-22T01:00:00",
        error_message="测试失败",
    )

    events = read_memory_events(project, event_type="task.updated")
    assert len(events) == 1
    assert events[0]["details"]["status"] == "failed"

    candidate = read_memory_candidates(project)[0]
    assert candidate["feedback"] == "negative"
    assert candidate["score"] <= 30
    assert "task_failed" in candidate["signals"]


def test_archiving_done_task_creates_positive_memory_feedback(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    task = db.create_task(project["name"], "归档巡检建议", content="do it", source="inspect")
    db.update_task(task["id"], status="done", completed_at="2026-05-22T01:00:00")

    db.update_task(task["id"], status="archived")

    events = read_memory_events(project, event_type="task.archived")
    assert len(events) == 1
    assert events[0]["details"]["task_id"] == task["id"]
    assert events[0]["details"]["status"] == "archived"

    candidate = next(item for item in read_memory_candidates(project) if item["event_type"] == "task.archived")
    assert candidate["feedback"] == "positive"
    assert candidate["score"] >= 80
    assert "task_archived" in candidate["signals"]


def test_retrying_failed_task_creates_retry_memory_feedback(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    task = db.create_task(project["name"], "重试巡检建议", content="do it", source="inspect")
    db.update_task(task["id"], status="failed", retry_count=2, error_message="boom")

    db.reset_task_for_retry(task["id"])

    events = read_memory_events(project, event_type="task.retried")
    assert len(events) == 1
    assert events[0]["details"]["task_id"] == task["id"]
    assert events[0]["details"]["from_status"] == "failed"
    assert events[0]["details"]["status"] == "backlog"

    candidate = next(item for item in read_memory_candidates(project) if item["event_type"] == "task.retried")
    assert candidate["feedback"] == "positive"
    assert 55 <= candidate["score"] < 80
    assert "task_retried" in candidate["signals"]


def test_deleting_task_creates_negative_memory_feedback(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    task = db.create_task(project["name"], "删除巡检建议", content="do it", source="inspect")

    assert db.delete_task(task["id"]) is True

    events = read_memory_events(project, event_type="task.deleted")
    assert len(events) == 1
    assert events[0]["details"]["task_id"] == task["id"]
    assert events[0]["details"]["status"] == "backlog"

    candidate = next(item for item in read_memory_candidates(project) if item["event_type"] == "task.deleted")
    assert candidate["feedback"] == "negative"
    assert candidate["score"] <= 20
    assert "task_deleted" in candidate["signals"]


def test_trace_includes_memory_events_by_default(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    append_memory_event(project, event_type="workflow.action_executed", source="test", summary="执行动作")

    result = CliRunner().invoke(main, ["trace", "-p", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert any(event["source"] == "memory" for event in payload["data"]["events"])


def test_ai_manifest_includes_memory_events_command():
    manifest = command_manifest(command_name="codepilot")
    assert any(item["command"] == "codepilot memory events -p <project-name> --json" for item in manifest["structured_outputs"])
    assert any(cmd["name"] == "memory" for cmd in manifest["commands"])
