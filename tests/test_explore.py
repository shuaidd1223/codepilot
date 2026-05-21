from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot.commands import explore as explore_cmd
from codepilot.ai_support.agent_support import command_manifest
from codepilot.cli import main
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _register_demo(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    db.register_project("demo", str(project))
    return project


def test_explore_json_returns_structured_file_evidence(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    (project / "codepilot").mkdir()
    (project / "codepilot" / "templates.py").write_text(
        "TASK_TEMPLATE_PATH = 'templates/task-template.md'\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        ["explore", "--project", "demo", "--prompt", "find task template", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["query"] == "find task template"
    assert data["rejected"] is False
    assert data["evidence"]
    assert data["sources"]
    assert any(item.get("kind") == "file_list" for item in data["evidence"])
    assert any(
        item.get("kind") == "file_search"
        and any("templates.py" in match["path"] for match in item.get("matches", []))
        for item in data["evidence"]
    )


def test_explore_rejects_write_or_execution_requests(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["explore", "--project", "demo", "--prompt", "delete a file", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["rejected"] is True
    assert payload["data"]["status"] == "rejected"
    assert payload["data"]["evidence"] == []
    assert "只读" in payload["data"]["limitations"][0]


def test_explore_file_search_falls_back_when_rg_is_unavailable(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    (project / "README.md").write_text("needle appears here\n", encoding="utf-8")
    monkeypatch.setattr("codepilot.commands.explore.shutil.which", lambda name: None)

    result = CliRunner().invoke(
        main,
        ["explore", "--project", "demo", "--prompt", "needle", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    matches = [
        match
        for item in payload["data"]["evidence"]
        if item.get("kind") == "file_search"
        for match in item.get("matches", [])
    ]
    assert any(match["path"] == "README.md" and "needle appears here" in match["line"] for match in matches)
    assert any("rg 不可用" in note for note in payload["data"]["limitations"])


def test_ai_manifest_includes_explore_command():
    manifest = command_manifest(command_name="codepilot")

    assert any(item["command"] == "codepilot explore --prompt <question> --json" for item in manifest["structured_outputs"])
    assert any(cmd["name"] == "explore" for cmd in manifest["commands"])


def test_git_show_evidence_only_accepts_commit_hashes(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    calls: list[list[str]] = []

    def _fake_run(args, root, *, timeout=8):
        calls.append(args)
        return 0, "readonly output", ""

    monkeypatch.setattr(explore_cmd, "_run_readonly_command", _fake_run)

    evidence, sources = explore_cmd._git_evidence(tmp_path, "show abc1234 and HEAD", [])

    assert any(item.get("kind") == "git_show" and item.get("ref") == "abc1234" for item in evidence)
    assert any(source.get("command", "").endswith("abc1234") for source in sources)
    assert not any("HEAD" in " ".join(call) for call in calls)
