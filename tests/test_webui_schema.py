"""Tests that webui_payloads actually produces the shape declared in
:mod:`codepilot.webui_schema`.

Without this contract, backend changes can silently strand the frontend
(the Vue components read keys that may no longer exist). The assertion
goes both ways: any key emitted by the payload must be in the TypedDict,
and any key declared in the TypedDict must be in the payload.
"""

from __future__ import annotations

import pytest

from codepilot import db
from codepilot import webui_payloads
from codepilot.webui_schema import (
    DashboardPayload,
    ProjectStats,
    ProjectSummary,
    TaskActions,
    TaskDetail,
    TaskListItem,
    payload_keys,
    to_json_schema,
)


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))
    task = db.create_task(
        "demo",
        "Schema smoke task",
        content="body",
        agent="codex",
        priority="P2",
    )
    return {"project": "demo", "task_id": task["id"]}


def test_task_list_item_matches_task_payload(fresh_db):
    task = db.get_task(fresh_db["task_id"])
    payload = webui_payloads._task_payload(task)
    assert set(payload.keys()) == payload_keys(TaskListItem)


def test_task_actions_match(fresh_db):
    task = db.get_task(fresh_db["task_id"])
    payload = webui_payloads._task_payload(task)
    assert set(payload["actions"].keys()) == payload_keys(TaskActions)


def test_task_detail_includes_list_item_keys(fresh_db):
    payload = webui_payloads.task_detail_payload(fresh_db["task_id"])
    assert payload_keys(TaskListItem).issubset(payload.keys())
    assert set(payload.keys()) == payload_keys(TaskDetail)


def test_dashboard_payload_shape(fresh_db, monkeypatch):
    # Stub out list_ui_jobs / list_ui_events — they depend on the webui
    # service module state which we don't need to exercise here.
    import codepilot.webui as webui

    monkeypatch.setattr(webui, "list_ui_jobs", lambda project: [], raising=False)
    monkeypatch.setattr(webui, "list_ui_events", lambda project: [], raising=False)

    payload = webui_payloads.dashboard_payload(fresh_db["project"])
    assert set(payload.keys()) == payload_keys(DashboardPayload)
    assert payload["tasks"]
    assert set(payload["tasks"][0].keys()) == payload_keys(TaskListItem)


def test_project_stats_keys(fresh_db):
    stats = db.get_task_stats(fresh_db["project"])
    assert set(stats.keys()) == payload_keys(ProjectStats)


def test_project_summary_keys(fresh_db):
    project = db.get_project(fresh_db["project"])
    summary = webui_payloads.project_summary(project)
    assert set(summary.keys()) == payload_keys(ProjectSummary)


def test_to_json_schema_captures_required_fields():
    schema = to_json_schema(TaskListItem)
    assert schema["type"] == "object"
    for field in ("id", "project", "title", "status", "actions"):
        assert field in schema["properties"]
    # required mirrors declared keys for a fully-typed dict.
    assert set(schema["required"]) == payload_keys(TaskListItem)
