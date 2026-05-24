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


def test_setup_does_not_materialize_default_skill_catalog(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)

    catalog_path = project / ".codepilot" / "skills" / "catalog.json"

    assert not catalog_path.exists()
    listed = CliRunner().invoke(main, ["skill", "list", "-p", "project", "--json"])
    assert listed.exit_code == 0, listed.output
    skills = json.loads(listed.output)["data"]["skills"]
    names = {item["name"] for item in skills}
    assert {"ralplan", "ralph", "build-fix", "wiki"} <= names
    build_fix = next(item for item in skills if item["name"] == "build-fix")
    assert {"codex", "claude", "gemini", "custom"} <= set(build_fix["supported_providers"])
    assert build_fix["entrypoint_command"] == "build-fix"
    assert build_fix["requires_enabled"] is True


def test_skill_list_search_show_enable_disable_json(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)
    runner = CliRunner()

    listed = runner.invoke(main, ["skill", "list", "-p", "project", "--json"])
    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert listed_payload["command"] == "skill list"
    assert any(item["name"] == "build-fix" for item in listed_payload["data"]["skills"])

    searched = runner.invoke(main, ["skill", "search", "quality", "-p", "project", "--json"])
    assert searched.exit_code == 0, searched.output
    searched_payload = json.loads(searched.output)
    assert any(item["name"] == "build-fix" for item in searched_payload["data"]["skills"])

    shown = runner.invoke(main, ["skill", "show", "build-fix", "-p", "project", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["data"]["skill"]["name"] == "build-fix"

    enabled = runner.invoke(main, ["skill", "enable", "build-fix", "-p", "project", "--json"])
    assert enabled.exit_code == 0, enabled.output
    assert json.loads(enabled.output)["data"]["skill"]["enabled"] is True

    disabled = runner.invoke(main, ["skill", "disable", "build-fix", "-p", "project", "--json"])
    assert disabled.exit_code == 0, disabled.output
    assert json.loads(disabled.output)["data"]["skill"]["enabled"] is False


def test_skill_run_rejects_disabled_skill(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["skill", "run", "ralplan", "-p", "project", "--input", "add wiki context", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "skill_catalog_error"


def test_skill_run_routes_enabled_builtin_to_current_codepilot_capability(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    runner = CliRunner()
    enabled = runner.invoke(main, ["skill", "enable", "ralplan", "-p", "project", "--json"])
    assert enabled.exit_code == 0, enabled.output

    result = runner.invoke(
        main,
        ["skill", "run", "ralplan", "-p", "project", "--provider", "claude", "--input", "add wiki context", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "skill run"
    assert payload["data"]["skill"]["name"] == "ralplan"
    assert payload["data"]["provider"] == "claude"
    assert payload["data"]["entrypoint_command"] == "plan"
    assert Path(payload["data"]["result"]["plan_path"]).is_file()
    assert Path(payload["data"]["result"]["plan_path"]).is_relative_to(project)
