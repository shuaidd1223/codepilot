from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db


def _setup_project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    db.init_db()

    project = tmp_path / "project"
    project.mkdir()
    config = project / "AGENTS.toml"
    config.write_text(
        """
[project]
name = "demo"

[agents.commands]
codex = "codex-bin"

[automation.scheduled_agents.task_health]
agent = "codex"
interval = "1h"
prompt = "Sensitive diagnostic prompt that should not be printed in full."
max_daily_cost_usd = 1.0

[automation.scheduled_agents.nightly_plan]
enabled = false
agent = "codex"
schedule = "0 2 * * *"
prompt = "Prepare tomorrow's plan."
""".strip()
        + "\n",
        encoding="utf-8",
    )
    db.register_project("demo", str(project), config_file=str(config))
    return project


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_scheduled_list_show_and_run_once_dry_run(tmp_path: Path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    runner = CliRunner()

    listed = _json(runner.invoke(main, ["scheduled", "list", "-p", "demo", "--json"]))
    agents = {item["name"]: item for item in listed["data"]["agents"]}

    assert agents["task_health"]["agent"] == "codex"
    assert agents["task_health"]["enabled"] is True
    assert agents["task_health"]["interval"] == "1h"
    assert agents["nightly_plan"]["enabled"] is False
    assert agents["nightly_plan"]["schedule"] == "0 2 * * *"

    shown_result = runner.invoke(main, ["scheduled", "show", "task_health", "-p", "demo", "--json"])
    shown = _json(shown_result)
    agent = shown["data"]["agent"]
    assert agent["name"] == "task_health"
    assert agent["prompt_length"] > len(agent["prompt_preview"])
    assert "Sensitive diagnostic prompt that should not be printed in full." not in shown_result.output

    run = _json(runner.invoke(main, ["scheduled", "run-once", "task_health", "-p", "demo", "--dry-run", "--json"]))
    assert run["data"]["result"]["dry_run"] is True
    assert run["data"]["result"]["exit_code"] is None
    assert run["data"]["result"]["command"][0] == "codex-bin"
    assert run["data"]["job"]["trigger"]["type"] == "manual"


def test_scheduled_disable_marks_agent_and_future_runs_skip(tmp_path: Path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    runner = CliRunner()

    disabled = _json(runner.invoke(main, ["scheduled", "disable", "task_health", "-p", "demo", "--json"]))
    assert disabled["data"]["agent"]["enabled"] is False
    assert disabled["data"]["agent"]["disabled_reason"] == "manual"

    listed = _json(runner.invoke(main, ["scheduled", "list", "-p", "demo", "--json"]))
    task_health = [item for item in listed["data"]["agents"] if item["name"] == "task_health"][0]
    assert task_health["enabled"] is False
    assert task_health["disabled_reason"] == "manual"

    run = _json(runner.invoke(main, ["scheduled", "run-once", "task_health", "-p", "demo", "--dry-run", "--json"]))
    assert run["data"]["result"]["guard_status"] == "skipped"
    assert run["data"]["result"]["guard_reason"] == "disabled"
