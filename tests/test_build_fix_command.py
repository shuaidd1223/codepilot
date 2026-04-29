from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands import build_fix as build_fix_mod
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def test_build_fix_retries_failed_task_runs_backlog_and_verification(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task(
        "demo",
        "repair me",
        content="""
## Verification Matrix

| AC # | Criterion | Verification Command / Action | Expected Result | Evidence Location |
| :--- | :--- | :--- | :--- | :--- |
| AC-1 | works | ignored-from-content | pass | terminal |
""".strip(),
    )
    db.update_task(task["id"], status="failed", retry_count=1, error_message="tests failed")

    calls: list[dict] = []

    def fake_run_backlog(project: str, **kwargs):
        calls.append({"project": project, **kwargs})
        db.update_task(task["id"], status="done", delivery_record="fixed")
        return {"processed": 1, "done": 1, "failed": 0, "requeued": 0, "cancelled": 0, "executor": "builtin"}

    monkeypatch.setattr(build_fix_mod, "run_backlog", fake_run_backlog)

    result = CliRunner().invoke(
        main,
        [
            "build-fix",
            "-p",
            "demo",
            "--task-id",
            str(task["id"]),
            "--verify-command",
            f"{sys.executable} -c \"print('ok')\"",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    data = payload["data"]
    assert payload["ok"] is True
    assert data["task_id"] == task["id"]
    assert data["triage"]["status"] == "failed"
    assert data["triage"]["error_message"] == "tests failed"
    assert [item["type"] for item in data["actions"]] == ["retry", "run"]
    assert calls == [
        {
            "project": "demo",
            "once": True,
            "limit": 1,
            "retry_on_failure": False,
            "quiet": True,
            "executor": "auto",
            "auto_commit": True,
        }
    ]
    assert data["verification"][0]["command"].endswith("print('ok')\"")
    assert data["verification"][0]["exit_code"] == 0
    assert data["verdict"] == "pass"


def test_build_fix_returns_json_error_when_no_failed_task(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    result = CliRunner().invoke(main, ["build-fix", "-p", "demo", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "build_fix_error"
