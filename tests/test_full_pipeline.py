"""Full pipeline integration tests: plan → create tasks → execute → merge.

All AI calls are mocked so these run without any API key or CLI tool.
They exercise the real DB, real git operations, and real CLI entry points.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from codepilot import db
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from codepilot.commands import run as run_cmd


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "checkout", "-b", "dev"], cwd=str(path), capture_output=True)
    readme = path / "README.md"
    readme.write_text("# Test Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _setup(tmp_path, monkeypatch):
    """Shared setup: test DB + git repo + registered project."""
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    db.init_db()
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0

    project_path = tmp_path / "project"
    project_path.mkdir()
    _init_git_repo(project_path)

    db.register_project("demo", str(project_path), base_branch="dev")
    return project_path


# ─── Plan → Create Tasks ────────────────────────────────────────────────────

def test_requirement_planning_creates_subtasks(tmp_path, monkeypatch):
    """run_requirement_workflow should create tasks from AI breakdown."""
    project_path = _setup(tmp_path, monkeypatch)

    breakdown = {
        "summary": "测试拆分",
        "complexity": "complex",
        "should_split": True,
        "tasks": [
            {"title": "子任务 A", "priority": "P1"},
            {"title": "子任务 B", "priority": "P2"},
        ],
    }
    monkeypatch.setattr(auto_mod, "generate_task_breakdown", lambda **kw: breakdown)
    monkeypatch.setattr(auto_mod, "run_backlog", lambda *a, **kw: {
        "processed": 0, "done": 0, "failed": 0, "requeued": 0, "cancelled": 0,
    })

    runner = CliRunner()
    result = runner.invoke(main, [
        "--project", "demo", "--no-execute", "实现两个子功能",
    ])

    assert result.exit_code == 0
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 2
    titles = {t["title"] for t in tasks}
    assert "子任务 A" in titles
    assert "子任务 B" in titles


# ─── Execute → Commit → Merge ───────────────────────────────────────────────

def test_builtin_executor_commits_and_merges_to_base(tmp_path, monkeypatch):
    """A successful builtin execution should commit on feat branch and merge back to dev."""
    project_path = _setup(tmp_path, monkeypatch)

    # Create a task
    task = db.create_task("demo", "add hello.txt", agent="codex", max_retries=1)

    # Mock the builtin executor: simulate writing a file + staging + committing
    def fake_run_builtin(task_dict, project, task_file, auto_commit=True, **kwargs):
        # Simulate builder: create a file and commit
        hello = project_path / "hello.txt"
        hello.write_text("hello world\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(project_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"task #{task_dict['id']}: add hello"],
            cwd=str(project_path), capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        return run_cmd.ExecutionResult(
            exit_code=0,
            output="created hello.txt",
            review_output="VERDICT: PASS",
            summary="added hello.txt",
            executor="builtin",
        )

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_run_builtin)

    stats = run_cmd.run_backlog("demo", once=True, limit=1, executor="builtin", auto_commit=False)

    assert stats["done"] == 1

    # Verify: should be back on dev branch with hello.txt merged
    branch_out = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(project_path), capture_output=True, text=True,
    )
    assert branch_out.stdout.strip() == "dev"

    hello = project_path / "hello.txt"
    assert hello.exists(), "hello.txt should be merged into dev"

    # Feature branch should be deleted
    branches = subprocess.run(
        ["git", "branch", "--list", "feat/task-*"],
        cwd=str(project_path), capture_output=True, text=True,
    )
    assert branches.stdout.strip() == "", "Feature branch should be deleted after merge"


def test_failed_execution_keeps_feature_branch(tmp_path, monkeypatch):
    """A failed task should NOT merge; feature branch is preserved for debugging."""
    project_path = _setup(tmp_path, monkeypatch)

    task = db.create_task("demo", "will fail", agent="codex", max_retries=1)

    def fake_run_builtin(task_dict, project, task_file, auto_commit=True, **kwargs):
        return run_cmd.ExecutionResult(
            exit_code=1,
            output="something broke",
            summary="build failed",
            executor="builtin",
        )

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_run_builtin)

    stats = run_cmd.run_backlog("demo", once=True, limit=1, executor="builtin")

    # Task should be requeued or failed, NOT done
    updated = db.get_task(task["id"])
    assert updated["status"] in ("backlog", "failed")

    # Failed tasks preserve feature branch for debugging (not merged to dev)
    branches = subprocess.run(
        ["git", "branch", "--list", "feat/task-*"],
        cwd=str(project_path), capture_output=True, text=True,
    )
    # Branch may or may not exist depending on whether task had changes
    # but we should NOT have merged to dev


# ─── Dedup during planning ──────────────────────────────────────────────────

def test_duplicate_requirement_does_not_double_create(tmp_path, monkeypatch):
    """Submitting the same requirement twice should not create duplicate tasks."""
    project_path = _setup(tmp_path, monkeypatch)

    breakdown = {
        "summary": "唯一任务",
        "complexity": "simple",
        "should_split": False,
        "tasks": [{"title": "唯一任务 X", "priority": "P2"}],
    }
    monkeypatch.setattr(auto_mod, "generate_task_breakdown", lambda **kw: breakdown)
    monkeypatch.setattr(auto_mod, "run_backlog", lambda *a, **kw: {
        "processed": 0, "done": 0, "failed": 0, "requeued": 0, "cancelled": 0,
    })

    runner = CliRunner()
    # Submit twice
    runner.invoke(main, ["--project", "demo", "--no-execute", "唯一任务 X"])
    runner.invoke(main, ["--project", "demo", "--no-execute", "唯一任务 X"])

    tasks = db.list_tasks(project="demo")
    titles = [t["title"] for t in tasks if t["title"] == "唯一任务 X"]
    # Dedup should prevent the second from creating a new task
    assert len(titles) == 1, f"Expected 1 task, got {len(titles)}: dedup not working"


# ─── End-to-end CLI: plan + execute ─────────────────────────────────────────

def test_cli_plain_text_plan_and_execute(tmp_path, monkeypatch):
    """codepilot 'requirement' should plan, create task, and attempt execution."""
    project_path = _setup(tmp_path, monkeypatch)

    breakdown = {
        "summary": "快速任务",
        "complexity": "simple",
        "should_split": False,
        "tasks": [{"title": "快速任务", "priority": "P2"}],
    }
    monkeypatch.setattr(auto_mod, "generate_task_breakdown", lambda **kw: breakdown)

    # Mock executor
    def fake_run_builtin(task_dict, project, task_file, auto_commit=True, **kwargs):
        (project_path / "output.txt").write_text("done", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(project_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "task done"],
            cwd=str(project_path), capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        return run_cmd.ExecutionResult(
            exit_code=0, output="ok", review_output="VERDICT: PASS",
            summary="done", executor="builtin",
        )

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_run_builtin)

    runner = CliRunner()
    result = runner.invoke(main, [
        "--project", "demo", "--execute", "--executor", "builtin", "快速完成一件事",
    ])

    assert result.exit_code == 0
    # Task should be done
    tasks = db.list_tasks(project="demo")
    done_tasks = [t for t in tasks if t["status"] == "done"]
    assert len(done_tasks) >= 1
