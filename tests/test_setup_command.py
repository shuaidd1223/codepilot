from __future__ import annotations

import json
import tomllib
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db


def _init_test_env(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))


def test_setup_dry_run_json_reports_actions_without_writing_project_files(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    codex_dir = project / ".codex"
    codex_dir.mkdir()
    hooks_file = codex_dir / "hooks.json"
    hooks_file.write_text('{"existing": true}', encoding="utf-8")

    result = CliRunner().invoke(main, ["setup", str(project), "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "setup"
    assert payload["ok"] is True
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["project"]["name"] == "project"
    assert not (project / "AGENTS.toml").exists()
    assert not (project / ".codepilot").exists()
    assert not (tmp_path / "tasks.db").exists()
    assert hooks_file.read_text(encoding="utf-8") == '{"existing": true}'

    actions = {(item["kind"], item["relative_path"]): item["status"] for item in payload["data"]["actions"]}
    assert actions[("config", "AGENTS.toml")] == "would_create"
    assert actions[("directory", ".codepilot/hooks")] == "would_create"
    assert actions[("codex_hooks", ".codex/hooks.json")] == "skipped"


def test_setup_creates_project_codepilot_layout_config_and_registration(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "demo"
    project.mkdir()
    (project / ".gitignore").write_text(".env*\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["setup", str(project), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["dry_run"] is False
    assert payload["data"]["project"]["name"] == "demo"
    assert payload["data"]["project"]["path"] == str(project.resolve())

    for relative in (
        ".codepilot",
        ".codepilot/state",
        ".codepilot/specs",
        ".codepilot/plans",
        ".codepilot/wiki",
        ".codepilot/hooks",
        ".codepilot/events",
    ):
        assert (project / relative).is_dir()
    assert not (project / ".codepilot" / "events" / "sinks.json").exists()
    assert not (project / ".codepilot" / "skills" / "catalog.json").exists()

    config_text = (project / "AGENTS.toml").read_text(encoding="utf-8")
    assert 'name = "demo"' in config_text
    assert "app_secret =" not in config_text
    gitignore_lines = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env*" in gitignore_lines
    assert gitignore_lines.count("AGENTS.toml") == 1
    registered = db.get_project("demo")
    assert registered is not None
    assert registered["path"] == str(project.resolve())
    assert not (project / ".codex" / "hooks.json").exists()

    statuses = {item["status"] for item in payload["data"]["actions"]}
    assert "created" in statuses
    assert "registered" in statuses


def test_setup_adds_agents_toml_to_gitignore_once(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "demo"
    project.mkdir()

    first = CliRunner().invoke(main, ["setup", str(project), "--json"])
    second = CliRunner().invoke(main, ["setup", str(project), "--json"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    gitignore_lines = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert gitignore_lines.count("AGENTS.toml") == 1

    payload = json.loads(second.output)
    assert any(
        item["kind"] == "gitignore" and item["status"] == "exists"
        for item in payload["data"]["actions"]
    )


def test_setup_is_idempotent_and_preserves_existing_codex_hooks(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "demo"
    project.mkdir()
    (project / ".codepilot" / "wiki").mkdir(parents=True)
    (project / ".codex").mkdir()
    hooks_file = project / ".codex" / "hooks.json"
    hooks_file.write_text('{"userHook": "keep"}', encoding="utf-8")

    first = CliRunner().invoke(main, ["setup", str(project), "--json"])
    second = CliRunner().invoke(main, ["setup", str(project), "--json"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["ok"] is True
    assert hooks_file.read_text(encoding="utf-8") == '{"userHook": "keep"}'
    assert db.get_project("demo") is not None
    assert any(
        item["kind"] == "codex_hooks" and item["status"] == "skipped"
        for item in payload["data"]["actions"]
    )
    assert any(
        item["kind"] == "config" and item["status"] == "exists"
        for item in payload["data"]["actions"]
    )
    assert any(
        item["relative_path"] == ".codepilot/wiki" and item["status"] == "exists"
        for item in payload["data"]["actions"]
    )


def test_setup_refreshes_parseable_existing_agents_toml(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "legacy"
    project.mkdir()
    (project / "AGENTS.toml").write_text(
        """
[project]
name = "legacy"
base_branch = "main"

[automation]
planner = "claude"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["setup", str(project), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    config_action = next(item for item in payload["data"]["actions"] if item["kind"] == "config")
    assert config_action["status"] == "refreshed"

    parsed = tomllib.loads((project / "AGENTS.toml").read_text(encoding="utf-8"))
    assert parsed["project"]["name"] == "legacy"
    assert parsed["project"]["base_branch"] == "main"
    assert parsed["agents"]["commands"]["codex"] == "codex"
    assert parsed["automation"]["planner"] == "claude"
    assert "inspect" in parsed


def test_setup_migrates_inline_secret_and_ignores_secrets_file(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "legacy-secret"
    project.mkdir()
    (project / "AGENTS.toml").write_text(
        """
[project]
name = "legacy-secret"

[feishu_bot]
enabled = true
app_id = "cli-demo"
app_secret = "feishu-inline-secret"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["setup", str(project), "--json"])

    assert result.exit_code == 0, result.output
    secrets_path = project / ".codepilot.secrets.toml"
    assert secrets_path.is_file()
    assert "feishu-inline-secret" in secrets_path.read_text(encoding="utf-8")
    assert ".codepilot.secrets.toml" in (project / ".gitignore").read_text(encoding="utf-8").splitlines()
