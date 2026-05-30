from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db


def _init_project(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("CodePilot self update validation\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["setup", str(project), "--json"])
    assert result.exit_code == 0, result.output
    return project


def test_self_update_dry_run_json_collects_preflight_evidence_and_plan(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)
    task = db.create_task("project", "failed task", agent="codex")
    db.update_task(task["id"], status="failed", error_message="boom")

    result = CliRunner().invoke(
        main,
        ["self-update", "-p", "project", "--dry-run", "--json", "improve self iteration"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    data = payload["data"]
    assert payload["command"] == "self-update"
    assert data["project"]["name"] == "project"
    assert data["goal"] == "improve self iteration"
    assert data["verdict"] == "dry_run"
    assert "doctor" in data["preflight"]
    assert "hook" in data["preflight"]
    assert "event_schema" in data["preflight"]
    assert data["preflight"]["exec"][0]["provider"] == "codex"
    assert data["evidence"]["failed_tasks"][0]["title"] == "failed task"
    assert data["evidence"]["trace"]["count"] >= 1
    assert data["plan"]["summary"]
    assert data["verification"]
    assert any("plan -p project" in item["command"] for item in data["next_actions"])


def test_self_update_rejects_non_dry_run_without_side_effects(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["self-update", "-p", "project", "--json", "improve self iteration"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "self_update_error"
    assert db.list_tasks(project="project") == []
    assert list((project / ".codepilot" / "plans").glob("*.md")) == []
    assert list((project / ".codepilot" / "wiki").glob("*.md")) == []


def test_self_update_unknown_project_returns_json_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))

    result = CliRunner().invoke(
        main,
        ["self-update", "-p", "missing", "--dry-run", "--json", "improve"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "self_update_error"


def test_self_update_passes_multiple_providers_to_exec_preflight(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        [
            "self-update",
            "-p",
            "project",
            "--provider",
            "codex",
            "--provider",
            "gemini",
            "--dry-run",
            "--json",
            "check providers",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    providers = [item["provider"] for item in data["preflight"]["exec"]]
    assert providers == ["codex", "gemini"]
    assert all(item["dry_run"] is True for item in data["preflight"]["exec"])
