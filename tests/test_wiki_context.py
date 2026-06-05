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
    result = CliRunner().invoke(main, ["init", str(project)])
    assert result.exit_code == 0, result.output
    return project


def test_wiki_update_refresh_delete_lifecycle(tmp_path, monkeypatch):
    project = _init_project(tmp_path, monkeypatch)
    runner = CliRunner()
    added = runner.invoke(
        main,
        ["wiki", "add", "-p", "project", "--title", "Build", "--body", "pytest tests", "--slug", "build", "--json"],
    )
    assert added.exit_code == 0, added.output

    updated = runner.invoke(
        main,
        [
            "wiki",
            "update",
            "-p",
            "project",
            "--slug",
            "build",
            "--title",
            "Build Commands",
            "--body",
            "pytest -q",
            "--json",
        ],
    )
    assert updated.exit_code == 0, updated.output
    payload = json.loads(updated.output)
    assert payload["data"]["page"]["title"] == "Build Commands"
    assert "pytest -q" in (project / ".codepilot" / "wiki" / "build.md").read_text(encoding="utf-8")

    refreshed = runner.invoke(main, ["wiki", "refresh", "-p", "project", "--json"])
    assert refreshed.exit_code == 0, refreshed.output
    assert json.loads(refreshed.output)["data"]["page_count"] == 1

    deleted = runner.invoke(main, ["wiki", "delete", "-p", "project", "--slug", "build", "--json"])
    assert deleted.exit_code == 0, deleted.output
    assert not (project / ".codepilot" / "wiki" / "build.md").exists()


def test_explore_includes_readonly_wiki_context_when_enabled(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)
    runner = CliRunner()
    added = runner.invoke(
        main,
        ["wiki", "add", "-p", "project", "--title", "Build", "--body", "Use pytest -q for validation.", "--json"],
    )
    assert added.exit_code == 0, added.output

    result = runner.invoke(main, ["explore", "-p", "project", "--use-wiki", "--json", "--prompt", "pytest validation"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    wiki = next(item for item in payload["data"]["evidence"] if item["kind"] == "wiki_context")
    assert wiki["results"][0]["path"].endswith(".md")
    assert any(source["type"] == "wiki" for source in payload["data"]["sources"])


def test_plan_includes_readonly_wiki_context_when_enabled(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)
    runner = CliRunner()
    added = runner.invoke(
        main,
        ["wiki", "add", "-p", "project", "--title", "Hooks", "--body", "Hook validation must not edit global configs.", "--json"],
    )
    assert added.exit_code == 0, added.output

    result = runner.invoke(main, ["plan", "-p", "project", "--use-wiki", "--json", "improve hook validation"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["wiki_context"]["enabled"] is True
    assert payload["data"]["wiki_context"]["results"][0]["path"].endswith(".md")
