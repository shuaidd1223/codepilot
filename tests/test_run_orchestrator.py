from __future__ import annotations

from pathlib import Path

from codepilot import db
from codepilot import webui as webui_mod
from codepilot.commands import run as run_cmd
from codepilot.commands import run_orchestrator as run_orchestrator_mod


def _init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    db.init_db()
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0


def _register_project_with_config(tmp_path: Path) -> Path:
    project_path = tmp_path / "project"
    project_path.mkdir()
    config_file = project_path / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
base_branch = "dev"

[automation]
per_task_branch = false
task_workspace = "branch"
""".strip(),
        encoding="utf-8",
    )
    db.register_project("demo", str(project_path), base_branch="dev", config_file=str(config_file))
    return project_path


def test_run_backlog_quiet_mode_skips_dashboard_render_on_success(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    _register_project_with_config(tmp_path)
    db.create_task("demo", "quiet success", agent="claude", max_retries=2)

    render_calls: list[dict] = []
    monkeypatch.setattr(
        run_cmd,
        "render_project_dashboard",
        lambda *args, **kwargs: render_calls.append({"args": args, "kwargs": kwargs}),
    )
    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=0,
            output="ok",
            summary="done",
            executor="builtin",
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)

    assert stats["done"] == 1
    assert render_calls == []


def test_run_backlog_quiet_mode_skips_dashboard_render_on_preflight_requeue(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    _register_project_with_config(tmp_path)
    task = db.create_task("demo", "quiet preflight", agent="dual", max_retries=2)

    render_calls: list[dict] = []
    monkeypatch.setattr(
        run_cmd,
        "render_project_dashboard",
        lambda *args, **kwargs: render_calls.append({"args": args, "kwargs": kwargs}),
    )
    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "preflight blocked")

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)
    current = db.get_task(task["id"])

    assert stats["processed"] == 1
    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert "preflight blocked" in (current["error_message"] or "")
    assert render_calls == []


def test_run_backlog_continues_after_requeued_failure_without_reselecting_same_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    _register_project_with_config(tmp_path)
    first = db.create_task("demo", "first fails once", agent="claude", max_retries=3)
    second = db.create_task("demo", "second still runs", agent="claude", max_retries=3)

    calls: list[int] = []

    def fake_executor(task, *args, **kwargs):
        calls.append(task["id"])
        if task["id"] == first["id"]:
            return run_cmd.ExecutionResult(exit_code=1, output="builder failed", executor="builtin")
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_executor)
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *args, **kwargs: None)

    stats = run_cmd.run_backlog("demo", once=False, limit=2, executor="builtin", auto_commit=False, quiet=True)

    assert calls == [first["id"], second["id"]]
    assert stats["processed"] == 2
    assert stats["requeued"] == 1
    assert stats["done"] == 1
    assert db.get_task(first["id"])["status"] == "backlog"
    assert db.get_task(first["id"])["retry_count"] == 1
    assert db.get_task(second["id"])["status"] == "done"


def test_run_backlog_continues_after_terminal_failure(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    _register_project_with_config(tmp_path)
    first = db.create_task("demo", "terminal failure", agent="claude", max_retries=1)
    second = db.create_task("demo", "runs after terminal failure", agent="claude", max_retries=3)

    def fake_executor(task, *args, **kwargs):
        if task["id"] == first["id"]:
            return run_cmd.ExecutionResult(exit_code=1, output="builder failed", executor="builtin")
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_executor)
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *args, **kwargs: None)

    stats = run_cmd.run_backlog("demo", once=False, limit=2, executor="builtin", auto_commit=False, quiet=True)

    assert stats["processed"] == 2
    assert stats["failed"] == 1
    assert stats["done"] == 1
    assert db.get_task(first["id"])["status"] == "failed"
    assert db.get_task(second["id"])["status"] == "done"


def test_prepare_task_workspace_resumes_existing_task_branch(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    task = {"id": 33, "title": "resume dirty branch", "agent": "claude"}
    expected_branch = run_cmd._task_branch_name(task["id"], task["title"])
    task["branch_name"] = expected_branch
    context = run_orchestrator_mod._RunContext(
        project={"name": "demo", "path": str(project_path)},
        project_path=project_path,
        config=None,
        base_branch="dev",
        shell_info=object(),
        executor="builtin",
        max_review_rounds=2,
        per_task_branch_enabled=True,
        task_workspace="branch",
    )

    monkeypatch.setattr(run_cmd, "_builtin_review_requires_git", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        run_cmd,
        "_builtin_preflight_error",
        lambda *args, **kwargs: "内置执行器检测到主工作区已有未提交改动。",
    )
    monkeypatch.setattr(run_cmd, "_git_current_branch", lambda _path: expected_branch)
    monkeypatch.setattr(
        run_cmd,
        "_git_prepare_task_branch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should reuse current task branch")),
    )

    workspace = run_orchestrator_mod._prepare_task_workspace(
        context,
        task,
        auto_commit=False,
        dry_run=False,
    )

    assert workspace.preflight_error == ""
    assert workspace.task_branch == expected_branch
    assert workspace.execution_path == project_path


def test_run_backlog_recovers_failed_dirty_task_branch_before_selecting_work(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path), base_branch="dev")
    task = db.create_task("demo", "resume failed dirty branch", agent="claude", max_retries=3)
    expected_branch = run_cmd._task_branch_name(task["id"], task["title"])
    db.update_task(
        task["id"],
        status="failed",
        retry_count=1,
        branch_name=expected_branch,
        worktree_path=str(project_path),
        error_message="review 未通过",
    )

    monkeypatch.setattr(run_cmd, "_git_is_repo", lambda _path: True)
    monkeypatch.setattr(run_cmd, "_git_has_changes", lambda _path: True)
    monkeypatch.setattr(run_cmd, "_git_current_branch", lambda _path: expected_branch)
    monkeypatch.setattr(run_cmd, "_builtin_review_requires_git", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        run_cmd,
        "_builtin_preflight_error",
        lambda *args, **kwargs: "内置执行器检测到主工作区已有未提交改动。",
    )
    monkeypatch.setattr(run_cmd, "_cleanup_worktree_leftovers", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        run_cmd,
        "_git_prepare_task_branch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should resume recovered branch")),
    )
    def fake_executor(*args, **kwargs):
        assert kwargs["allow_dirty_resume"] is True
        return run_cmd.ExecutionResult(
            exit_code=0,
            output="fixed",
            summary="done",
            executor="builtin",
        )

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_executor)

    stats = run_cmd.run_backlog("demo", once=True, limit=1, executor="builtin", auto_commit=False, quiet=True)
    current = db.get_task(task["id"])

    assert stats["done"] == 1
    assert current["status"] == "done"
