from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from codepilot.ai_support.agent_support import command_manifest
from codepilot.cli import main
from codepilot.commands.note import add_note, prune_working_notes, read_notepad, write_notepad
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _register_demo(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    return db.register_project("demo", str(project))


def test_note_add_show_and_clear_json(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)
    runner = CliRunner()

    add_result = runner.invoke(main, ["note", "add", "-p", "demo", "构建命令是 pytest tests", "--json"])
    assert add_result.exit_code == 0, add_result.output
    added = json.loads(add_result.output)
    assert added["data"]["section"] == "working"

    priority_result = runner.invoke(main, ["note", "add", "-p", "demo", "--priority", "项目使用 strict 模式"])
    assert priority_result.exit_code == 0, priority_result.output

    manual_result = runner.invoke(main, ["note", "add", "-p", "demo", "--manual", "发布前运行 binary verify"])
    assert manual_result.exit_code == 0, manual_result.output

    show_result = runner.invoke(main, ["note", "show", "-p", "demo", "--json"])
    assert show_result.exit_code == 0, show_result.output
    shown = json.loads(show_result.output)
    sections = shown["data"]["sections"]
    assert sections["working"][0]["content"] == "构建命令是 pytest tests"
    assert sections["priority"][0]["content"] == "项目使用 strict 模式"
    assert sections["manual"][0]["content"] == "发布前运行 binary verify"

    clear_result = runner.invoke(main, ["note", "clear", "-p", "demo", "--json"])
    assert clear_result.exit_code == 0, clear_result.output
    cleared = json.loads(clear_result.output)
    assert cleared["data"]["removed"] == 1

    after_clear = read_notepad(db.get_project("demo"))
    assert after_clear["sections"]["working"] == []
    assert after_clear["sections"]["priority"][0]["content"] == "项目使用 strict 模式"
    assert after_clear["sections"]["manual"][0]["content"] == "发布前运行 binary verify"


def test_note_prune_removes_old_working_entries_only(tmp_path, monkeypatch):
    project = _register_demo(tmp_path, monkeypatch)
    add_note(project, "保留 priority", section="priority")
    add_note(project, "保留 manual", section="manual")
    data = read_notepad(project)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fresh = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    data["sections"]["working"] = [
        {"timestamp": old, "content": "旧上下文"},
        {"timestamp": fresh, "content": "新上下文"},
    ]
    write_notepad(project, data)

    result = prune_working_notes(project, days=7)

    assert result["removed"] == 1
    pruned = read_notepad(project)
    assert [item["content"] for item in pruned["sections"]["working"]] == ["新上下文"]
    assert pruned["sections"]["priority"][0]["content"] == "保留 priority"
    assert pruned["sections"]["manual"][0]["content"] == "保留 manual"


def test_note_rejects_secret_like_content(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["note", "add", "-p", "demo", "FEISHU_APP_SECRET=abc123", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "secret" in payload["error"]["message"].lower()


def test_note_rejects_conflicting_sections(tmp_path, monkeypatch):
    _register_demo(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["note", "add", "-p", "demo", "--priority", "--manual", "冲突", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "--priority" in payload["error"]["message"]


def test_ai_manifest_includes_note_commands():
    manifest = command_manifest(command_name="codepilot")

    assert any(item["command"] == "codepilot note show -p <项目名> --json" for item in manifest["structured_outputs"])
    assert any(cmd["name"] == "note" for cmd in manifest["commands"])
