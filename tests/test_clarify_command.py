from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.ai_support.agent_support import command_manifest
from codepilot.core.workflow_state import read_workflow_state
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _register_demo(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("doctor command checks local services\n", encoding="utf-8")
    return db.register_project("demo", str(project))


def test_clarify_json_writes_spec_artifact_without_creating_tasks(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    before = db.get_task_stats("demo")["total"]

    result = CliRunner().invoke(main, ["clarify", "-p", "demo", "改进 doctor", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["summary"]
    assert data["open_questions"]
    spec_path = Path(data["artifact_path"])
    assert spec_path.exists()
    assert spec_path.is_relative_to(Path(project["path"]))
    assert db.get_task_stats("demo")["total"] == before

    text = spec_path.read_text(encoding="utf-8")
    for heading in ("## 目标", "## 范围", "## 非目标", "## 约束", "## 验收标准", "## 待确认问题"):
        assert heading in text
    assert "项目证据" in text


def test_clarify_falls_back_to_rule_based_spec_when_explore_fails(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("explore unavailable")

    monkeypatch.setattr("codepilot.commands.clarify.explore_project", boom)

    result = CliRunner().invoke(main, ["clarify", "-p", "demo", "--quick", "优化任务面板", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["evidence_count"] == 0
    spec_path = Path(payload["data"]["artifact_path"])
    assert "未收集到项目证据" in spec_path.read_text(encoding="utf-8")


def test_clarify_marks_workflow_state_complete(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["clarify", "-p", "demo", "梳理构建命令", "--json"])

    assert result.exit_code == 0, result.output
    state = read_workflow_state(project["path"], mode="clarify")
    assert state["active"] is False
    assert state["current_phase"] == "completed"
    assert state["artifact_paths"]["spec"] == json.loads(result.output)["data"]["artifact_path"]


def test_ai_manifest_includes_clarify_command():
    manifest = command_manifest(command_name="codepilot")

    assert any(cmd["name"] == "clarify" for cmd in manifest["commands"])
