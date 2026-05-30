from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _project(tmp_path: Path, monkeypatch) -> Path:
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def test_wiki_ingest_from_trace_writes_sourced_page(tmp_path, monkeypatch):
    project_path = _project(tmp_path, monkeypatch)
    task = db.create_task("demo", "trace task")
    db.update_task(task["id"], status="failed", error_message="pytest failed", completed_at="2026-04-29T10:00:00")

    result = CliRunner().invoke(main, ["wiki", "ingest", "--from", "trace", "-p", "demo", "--task", str(task["id"]), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    page = payload["data"]["page"]
    assert page["source"] == "wiki.ingest.trace"
    text = (project_path / ".codepilot" / "wiki" / page["path"]).read_text(encoding="utf-8")
    assert "source: wiki.ingest.trace" in text
    assert f"related_task: {task['id']}" in text
    assert "task.completed" in text
    assert "pytest failed" in text


def test_wiki_ingest_from_plan_writes_latest_plan_page(tmp_path, monkeypatch):
    project_path = _project(tmp_path, monkeypatch)
    plan_dir = project_path / ".codepilot" / "plans"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan-demo.md").write_text("# Execution Plan: demo\n\n## 验证矩阵\n\npytest -q\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["wiki", "ingest", "--from", "plan", "-p", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    page = payload["data"]["page"]
    assert page["source"] == "wiki.ingest.plan"
    text = (project_path / ".codepilot" / "wiki" / page["path"]).read_text(encoding="utf-8")
    assert "source: wiki.ingest.plan" in text
    assert "related_workflow: plan" in text
    assert "# Execution Plan: demo" in text
