from __future__ import annotations

import json
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


def test_hook_plan_reports_wrapper_without_touching_codex_hooks(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    (project / ".codex").mkdir()
    hooks_file = project / ".codex" / "hooks.json"
    hooks_file.write_text('{"existing": true}', encoding="utf-8")

    result = CliRunner().invoke(main, ["hook", "plan", "-p", "project", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "hook plan"
    assert payload["data"]["project"] == "project"
    assert payload["data"]["codex_hooks"]["status"] == "preserve_existing"
    assert payload["data"]["registry"]["hooks"][0]["name"] == "codepilot-event-wrapper"
    assert hooks_file.read_text(encoding="utf-8") == '{"existing": true}'
    assert not (project / ".codepilot" / "hooks" / "registry.json").exists()


def test_hook_install_dry_run_writes_nothing_and_reports_registry_plan(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["hook", "install", "-p", "project", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "hook install"
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["registry"]["hooks"][0]["enabled"] is True
    assert payload["data"]["actions"][0]["status"] == "would_write"
    assert not (project / ".codepilot" / "hooks" / "registry.json").exists()
    assert not (project / ".codex" / "hooks.json").exists()


def test_hook_install_without_dry_run_is_rejected(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["hook", "install", "-p", "project", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "hook_error"


def test_hook_uninstall_dry_run_reports_preserved_codex_hooks(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    (project / ".codepilot" / "hooks" / "registry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hooks": [
                    {
                        "name": "codepilot-event-wrapper",
                        "type": "codex-wrapper",
                        "enabled": True,
                        "targets": ["pre_tool_use", "post_tool_use"],
                        "wrapper": ".codepilot/hooks/codepilot-hook-wrapper",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["hook", "uninstall", "-p", "project", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "hook uninstall"
    assert payload["data"]["actions"][0]["status"] == "would_disable"
    assert payload["data"]["codex_hooks"]["status"] == "not_modified"
    registry = json.loads((project / ".codepilot" / "hooks" / "registry.json").read_text(encoding="utf-8"))
    assert registry["hooks"][0]["enabled"] is True


def test_hook_validate_reports_provider_neutral_lifecycle_without_touching_global_hooks(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    (project / ".codex").mkdir()
    hooks_file = project / ".codex" / "hooks.json"
    hooks_file.write_text('{"user": "kept"}', encoding="utf-8")

    result = CliRunner().invoke(main, ["hook", "validate", "-p", "project", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "hook validate"
    assert payload["data"]["valid"] is True
    assert {"codex", "claude", "gemini", "custom"} <= set(payload["data"]["providers"])
    assert "agent.prompt.submitted" in payload["data"]["lifecycle_events"]
    assert hooks_file.read_text(encoding="utf-8") == '{"user": "kept"}'


def test_hook_test_writes_project_local_log_and_dispatches_agent_event(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    register = CliRunner().invoke(
        main,
        [
            "event",
            "register",
            "-p",
            "project",
            "--name",
            "agent-audit",
            "--type",
            "jsonl",
            "--path",
            ".codepilot/events/agent.jsonl",
            "--event",
            "agent.prompt.submitted",
            "--json",
        ],
    )
    assert register.exit_code == 0, register.output

    result = CliRunner().invoke(
        main,
        ["hook", "test", "-p", "project", "--provider", "gemini", "--event", "agent.prompt.submitted", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "hook test"
    assert payload["data"]["event"]["payload"]["provider"] == "gemini"
    assert payload["data"]["logged"] is True
    assert payload["data"]["delivered"] == 1

    hook_lines = (project / ".codepilot" / "hooks" / "logs" / "hook-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(hook_lines) == 1
    assert json.loads(hook_lines[0])["type"] == "agent.prompt.submitted"
    event_lines = (project / ".codepilot" / "events" / "agent.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(event_lines[0])["payload"]["provider"] == "gemini"


def test_hook_logs_reads_project_local_hook_log(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    log_dir = project / ".codepilot" / "hooks" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "hook-events.jsonl").write_text(
        json.dumps({"type": "agent.run.failed", "payload": {"provider": "custom"}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["hook", "logs", "-p", "project", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "hook logs"
    assert payload["data"]["events"][0]["type"] == "agent.run.failed"
