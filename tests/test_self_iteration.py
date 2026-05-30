"""E2E tests for the self-iteration closed loop.

Validates the full lifecycle:
  task branch created -> builder modifies files -> auto-commit ->
  merge back to base_branch -> branch deleted -> task status=done

Uses ai._phase_stub to inject a stub provider so no external AI is needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codepilot.ai_support import service as ai_mod
from codepilot.storage import database as db
from codepilot.webapp import server as webui_mod
from codepilot.commands import run as run_cmd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    db.init_db()
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0


def _init_git_repo(project_path: Path, branch: str = "main") -> None:
    """Create a small Python project inside a fresh git repo."""
    def run(*args):
        return subprocess.run(
            list(args), cwd=project_path, check=True,
            capture_output=True, text=True,
        )

    run("git", "init", "-b", branch)
    run("git", "config", "user.name", "TestBot")
    run("git", "config", "user.email", "bot@test.local")

    (project_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (project_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    run("git", "add", "-A")
    run("git", "commit", "-m", "initial commit")


def _stub_phase_factory(project_path: Path):
    """Return a phase stub that appends a line to README (builder) or passes review."""

    def _stub(*, task, project_path, phase, prompt):  # noqa: ARG001
        if phase == "builder":
            readme = project_path / "README.md"
            text = readme.read_text(encoding="utf-8")
            readme.write_text(text + "Added by e2e test\n", encoding="utf-8")
            return "claude", 0, "Summary: appended line to README"
        # reviewer
        return "claude-review", 0, "No issues found.\nVERDICT: PASS"

    return _stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_self_iteration_full_lifecycle(tmp_path, monkeypatch):
    """Task flows through: branch -> build -> commit -> merge -> delete branch -> done."""
    project_path = tmp_path / "repo"
    project_path.mkdir()
    _init_git_repo(project_path, branch="main")

    _init_test_db(tmp_path, monkeypatch)
    db.register_project("e2e", str(project_path), base_branch="main")

    task = db.create_task(
        "e2e",
        "在 README 末尾添加一行",
        content="在 README.md 末尾添加: 'Added by e2e test'",
        agent="claude",
    )
    task_id = task["id"]

    # Inject stub provider -- no real AI needed
    monkeypatch.setattr(ai_mod, "_phase_stub", _stub_phase_factory(project_path))
    monkeypatch.setattr(
        ai_mod, "check_provider_availability",
        lambda agent, project_path=None: (True, "stub"),
    )

    # Execute
    stats = run_cmd.run_backlog("e2e", once=True, executor="builtin", auto_commit=True)

    # -- Assertions ----------------------------------------------------------

    # 1. Task status == done
    updated = db.get_task(task_id)
    assert updated["status"] == "done", f"expected done, got {updated['status']}"

    # 2. Feature branch was created (commit history should contain merge commit)
    # NOTE: on Windows default subprocess encoding is GBK; commit messages from
    # the builtin executor contain UTF-8 Chinese, so decoding fails silently
    # and stdout ends up None. Pin encoding/errors explicitly.
    log_output = subprocess.run(
        ["git", "log", "--oneline", "--all", "--graph"],
        cwd=project_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout or ""
    assert "task #" in log_output.lower() or "merge" in log_output.lower(), (
        f"Expected merge evidence in log:\n{log_output}"
    )

    # 3. README was actually modified
    readme = (project_path / "README.md").read_text(encoding="utf-8")
    assert "Added by e2e test" in readme

    # 4. Task branch deleted after merge
    branches = subprocess.run(
        ["git", "branch"], cwd=project_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout or ""
    assert f"feat/task-{task_id}" not in branches, (
        f"Task branch should have been deleted, but found:\n{branches}"
    )

    # 5. Currently back on the base branch
    current = (subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=project_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout or "").strip()
    assert current == "main", f"expected main, on {current}"

    # 6. Stats
    assert stats["done"] == 1
    assert stats["failed"] == 0


@pytest.mark.slow
def test_self_iteration_builder_failure_requeues(tmp_path, monkeypatch):
    """When the builder phase fails, the task should be requeued (not marked done)."""
    project_path = tmp_path / "repo"
    project_path.mkdir()
    _init_git_repo(project_path, branch="main")

    _init_test_db(tmp_path, monkeypatch)
    db.register_project("e2e-fail", str(project_path), base_branch="main")

    task = db.create_task(
        "e2e-fail",
        "故意失败的任务",
        content="should fail",
        agent="claude",
        max_retries=2,
    )
    task_id = task["id"]

    def _failing_stub(*, task, project_path, phase, prompt):  # noqa: ARG001
        if phase == "builder":
            return "claude", 1, "error: something went wrong"
        return "claude-review", 0, "VERDICT: PASS"

    monkeypatch.setattr(ai_mod, "_phase_stub", _failing_stub)
    monkeypatch.setattr(
        ai_mod, "check_provider_availability",
        lambda agent, project_path=None: (True, "stub"),
    )

    stats = run_cmd.run_backlog("e2e-fail", once=True, executor="builtin", auto_commit=True)

    updated = db.get_task(task_id)
    # Should NOT be done
    assert updated["status"] != "done"
    assert stats["done"] == 0


@pytest.mark.slow
def test_self_iteration_review_fail_not_merged(tmp_path, monkeypatch):
    """When the reviewer returns VERDICT: FAIL, changes must NOT be merged."""
    project_path = tmp_path / "repo"
    project_path.mkdir()
    _init_git_repo(project_path, branch="main")

    _init_test_db(tmp_path, monkeypatch)
    db.register_project("e2e-review", str(project_path), base_branch="main")

    task = db.create_task(
        "e2e-review",
        "review 不通过的任务",
        content="changes that fail review",
        agent="claude",
        max_retries=0,
    )
    task_id = task["id"]

    def _review_fail_stub(*, task, project_path, phase, prompt):  # noqa: ARG001
        if phase == "builder":
            readme = project_path / "README.md"
            text = readme.read_text(encoding="utf-8")
            readme.write_text(text + "bad change\n", encoding="utf-8")
            return "claude", 0, "Summary: made a bad change"
        return "claude-review", 0, "Found critical issues.\nVERDICT: FAIL"

    monkeypatch.setattr(ai_mod, "_phase_stub", _review_fail_stub)
    monkeypatch.setattr(
        ai_mod, "check_provider_availability",
        lambda agent, project_path=None: (True, "stub"),
    )

    stats = run_cmd.run_backlog(
        "e2e-review", once=True, executor="builtin",
        auto_commit=True, retry_on_failure=False,
    )

    updated = db.get_task(task_id)
    assert updated["status"] == "failed", f"expected failed, got {updated['status']}"

    # README on main should NOT contain the bad change.
    # Force-checkout to discard uncommitted working-tree changes left by the
    # builder stub (review rejected them, so they were never committed).
    subprocess.run(["git", "checkout", "-f", "main"], cwd=project_path, capture_output=True)
    readme = (project_path / "README.md").read_text(encoding="utf-8")
    assert "bad change" not in readme

    assert stats["done"] == 0
