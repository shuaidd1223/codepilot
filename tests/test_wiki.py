from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.ai_support.agent_support import command_manifest
from codepilot.commands.wiki import add_wiki_note, query_wiki
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _register_demo(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    return db.register_project("demo", str(project))


def test_wiki_add_list_and_query_json(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)
    runner = CliRunner()

    add_result = runner.invoke(
        main,
        ["wiki", "add", "-p", "demo", "--title", "构建命令", "--body", "pytest tests"],
    )
    assert add_result.exit_code == 0, add_result.output

    list_result = runner.invoke(main, ["wiki", "list", "-p", "demo", "--json"])
    assert list_result.exit_code == 0, list_result.output
    listed = json.loads(list_result.output)
    assert listed["ok"] is True
    assert listed["data"]["pages"][0]["title"] == "构建命令"

    query_result = runner.invoke(main, ["wiki", "query", "-p", "demo", "构建", "--json"])
    assert query_result.exit_code == 0, query_result.output
    queried = json.loads(query_result.output)
    assert queried["data"]["results"][0]["title"] == "构建命令"
    assert queried["data"]["results"][0]["score"] > 0


def test_wiki_query_supports_cjk_body_matches(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    add_wiki_note(project, title="巡检发现", body="飞书任务面板需要后台状态同步")

    results = query_wiki(project, "状态同步")

    assert results
    assert results[0]["title"] == "巡检发现"


def test_wiki_rejects_path_traversal_slug(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["wiki", "add", "-p", "demo", "--title", "Bad", "--body", "body", "--slug", "../outside", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "非法" in payload["error"]["message"]


def test_wiki_rejects_secret_like_content(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["wiki", "add", "-p", "demo", "--title", "密钥", "--body", "FEISHU_APP_SECRET=abc123", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "secret" in payload["error"]["message"].lower()


def test_wiki_lint_reports_missing_metadata(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    wiki_dir = tmp_path / "project" / ".codepilot" / "wiki"
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "broken.md").write_text("body only\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["wiki", "lint", "-p", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    issues = payload["data"]["issues"]
    assert any(issue["path"] == "broken.md" and issue["code"] == "missing_metadata" for issue in issues)
    assert payload["data"]["ok"] is False


def test_ai_manifest_includes_wiki_commands():
    manifest = command_manifest(command_name="codepilot")

    assert any(item["command"] == "codepilot wiki list -p <项目名> --json" for item in manifest["structured_outputs"])
    assert any(cmd["name"] == "wiki" for cmd in manifest["commands"])
