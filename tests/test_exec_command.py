from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main


def _init_project(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "project"
    project.mkdir()
    result = CliRunner().invoke(main, ["setup", str(project), "--json"])
    assert result.exit_code == 0, result.output
    return project


def test_exec_dry_run_reports_provider_preflight_without_running(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["exec", "-p", "project", "--provider", "custom", "--dry-run", "--json", "--", sys.executable, "-c", "print('no')"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "exec"
    assert payload["data"]["provider"] == "custom"
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["command"][0] == sys.executable
    assert payload["data"]["verdict"] == "dry_run"
    assert payload["data"]["preflight"]["project_configured"] is True


def test_exec_run_records_project_local_log_and_event(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    register = CliRunner().invoke(
        main,
        [
            "event",
            "register",
            "-p",
            "project",
            "--name",
            "exec-audit",
            "--type",
            "jsonl",
            "--path",
            ".codepilot/events/exec.jsonl",
            "--event",
            "exec.completed",
            "--json",
        ],
    )
    assert register.exit_code == 0, register.output

    result = CliRunner().invoke(
        main,
        ["exec", "-p", "project", "--provider", "custom", "--json", "--", sys.executable, "-c", "print('ok')"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["exit_code"] == 0
    assert payload["data"]["stdout_tail"] == "ok"
    assert payload["data"]["verdict"] == "pass"

    exec_lines = (project / ".codepilot" / "exec" / "exec.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(exec_lines) == 1
    assert json.loads(exec_lines[0])["provider"] == "custom"
    event_lines = (project / ".codepilot" / "events" / "exec.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(event_lines[0])["type"] == "exec.completed"
    assert json.loads(event_lines[0])["payload"]["provider"] == "custom"


def test_exec_rejects_unknown_provider(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["exec", "-p", "project", "--provider", "other", "--dry-run", "--json", "--", "echo"])

    assert result.exit_code != 0
