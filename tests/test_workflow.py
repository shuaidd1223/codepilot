from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codepilot import db
from codepilot.cli import main
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd


def _init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()


def test_update_task_allows_core_fields(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "old title", agent="dual", priority="P2", depends_on=[1])

    updated = db.update_task(
        task["id"],
        title="new title",
        content="new content",
        agent="codex",
        priority="P0",
        depends_on=[2, 3],
    )

    assert updated["title"] == "new title"
    assert updated["content"] == "new content"
    assert updated["agent"] == "codex"
    assert updated["priority"] == "P0"
    assert json.loads(updated["depends_on"]) == [2, 3]


def test_increment_task_retry_eventually_fails(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me", max_retries=2)

    first = db.increment_task_retry(task["id"], "boom")
    second = db.increment_task_retry(task["id"], "boom again")

    assert first["status"] == "backlog"
    assert first["retry_count"] == 1
    assert second["status"] == "failed"
    assert second["retry_count"] == 2


def test_auto_command_creates_linear_subtasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: {
            "summary": "split ok",
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a", "b"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                },
                {
                    "title": "step 2",
                    "priority": "P2",
                    "goal": "do step 2",
                    "acceptance_criteria": ["c", "d"],
                    "builder_notes": ["code 2"],
                    "reviewer_notes": ["review 2"],
                    "files": ["b.py"],
                    "notes": ["note 2"],
                },
            ],
        },
    )

    runner = CliRunner()
    result = runner.invoke(main, ["auto", "-p", "demo", "-t", "big goal", "--plan-only"])

    assert result.exit_code == 0
    tasks = sorted(db.list_tasks(project="demo"), key=lambda item: item["id"])
    assert len(tasks) == 2
    assert tasks[0]["title"] == "step 1"
    assert tasks[0]["depends_on"] is None
    assert tasks[1]["title"] == "step 2"
    assert json.loads(tasks[1]["depends_on"]) == [tasks[0]["id"]]


def test_run_backlog_builtin_stops_after_retry_limit(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=1, output="builder failed", executor="builtin"),
    )

    first = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    assert first["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1

    second = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    assert second["failed"] == 1
    assert current["status"] == "failed"
    assert current["retry_count"] == 2


def test_resolve_project_for_prompt_uses_current_directory(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    project = auto_cmd.resolve_project_for_prompt()

    assert project["name"] == "demo"
    assert project["path"] == str(project_path)


def test_root_command_accepts_plain_text_requirement(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    captured = {}

    def fake_run_requirement_workflow(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["--no-execute", "实现一个自动重试机制"])

    assert result.exit_code == 0
    assert captured["title"] == "实现一个自动重试机制"
    assert captured["project_info"]["name"] == "demo"
    assert captured["execute"] is False


def test_chat_command_accepts_plain_text_and_exit(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    captured = []

    def fake_run_requirement_workflow(**kwargs):
        captured.append(kwargs["title"])
        return {"ok": True}

    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="做一个自动重试机制\n/exit\n")

    assert result.exit_code == 0
    assert captured == ["做一个自动重试机制"]
    assert "CodePilot Chat" in result.output


def test_root_command_without_args_shows_help_in_non_interactive_mode():
    runner = CliRunner()
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    assert "Usage:" in result.output
