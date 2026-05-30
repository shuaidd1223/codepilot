from __future__ import annotations

from pathlib import Path

from codepilot.storage import database as db
from codepilot.webapp import server as webui_mod
from codepilot.commands import run as run_cmd
from codepilot.commands import run_orchestrator as run_orchestrator_mod


def _init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    db.init_db()
    monkeypatch.setattr(db, "_record_task_update_memory", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_orchestrator_mod, "_publish_web_task_event", lambda *args, **kwargs: None)
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


def _init_git_repo(project_path: Path) -> str:
    run_cmd._run_command(["git", "init"], cwd=project_path, timeout=60)
    run_cmd._run_command(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, timeout=30)
    run_cmd._run_command(["git", "config", "user.email", "test@example.com"], cwd=project_path, timeout=30)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    run_cmd._run_command(["git", "add", "README.md"], cwd=project_path, timeout=30)
    code, output = run_cmd._run_command(["git", "commit", "-m", "init"], cwd=project_path, timeout=120)
    assert code == 0, output
    return run_cmd._git_current_branch(project_path)


def _register_project_with_dirty_policy(tmp_path: Path, policy: str) -> Path:
    project_path = tmp_path / "project"
    project_path.mkdir()
    base_branch = _init_git_repo(project_path)
    config_file = project_path / "AGENTS.toml"
    config_file.write_text(
        f"""
[project]
name = "demo"
base_branch = "{base_branch}"

[automation]
per_task_branch = false
task_workspace = "direct"
preflight_dirty_worktree = "{policy}"
""".strip(),
        encoding="utf-8",
    )
    run_cmd._run_command(["git", "add", "AGENTS.toml"], cwd=project_path, timeout=30)
    code, output = run_cmd._run_command(["git", "commit", "-m", "add config"], cwd=project_path, timeout=120)
    assert code == 0, output
    db.register_project("demo", str(project_path), base_branch=base_branch, config_file=str(config_file))
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


def test_run_backlog_does_not_repeat_same_preflight_skip_notification(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    _register_project_with_config(tmp_path)
    db.create_task("demo", "repeat preflight", agent="dual", max_retries=2)

    notifications = []
    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "preflight blocked")
    monkeypatch.setattr(
        run_cmd,
        "notify_task_event",
        lambda *args, **kwargs: notifications.append(kwargs) or True,
    )

    first = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)
    second = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)

    assert first["requeued"] == 1
    assert second["requeued"] == 1
    assert [item["event"] for item in notifications] == ["preflight_skip"]


def test_run_backlog_default_dirty_worktree_policy_stops_even_without_auto_commit(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = _register_project_with_dirty_policy(tmp_path, "stop")
    (project_path / "README.md").write_text("# demo\nlocal edit\n", encoding="utf-8")
    task = db.create_task("demo", "blocked by dirty tree", agent="dual", max_retries=2)
    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executor should not start")),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert "未提交改动" in (current["error_message"] or "")


def test_run_backlog_dirty_worktree_policy_commit_saves_preflight_changes(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = _register_project_with_dirty_policy(tmp_path, "commit")
    (project_path / "README.md").write_text("# demo\nlocal edit\n", encoding="utf-8")
    (project_path / "scratch.txt").write_text("scratch\n", encoding="utf-8")
    task = db.create_task("demo", "commit dirty tree", agent="dual", max_retries=2)
    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin"),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)
    current = db.get_task(task["id"])
    code, log_output = run_cmd._run_command(["git", "log", "-1", "--pretty=%s%n%b"], cwd=project_path, timeout=30)
    status_code, status_output = run_cmd._run_command(["git", "status", "--short"], cwd=project_path, timeout=30)

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert code == 0
    assert f"codepilot preflight: save worktree before task #{task['id']}" in log_output
    assert "scratch.txt" in log_output
    assert status_code == 0
    assert status_output.strip() == ""


def test_run_backlog_dirty_worktree_policy_stash_records_preflight_log(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = _register_project_with_dirty_policy(tmp_path, "stash")
    (project_path / "README.md").write_text("# demo\nlocal edit\n", encoding="utf-8")
    (project_path / "scratch.txt").write_text("scratch\n", encoding="utf-8")
    task = db.create_task("demo", "stash dirty tree", agent="dual", max_retries=2)
    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin"),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)
    current = db.get_task(task["id"])
    status_code, status_output = run_cmd._run_command(["git", "status", "--short"], cwd=project_path, timeout=30)
    stash_code, stash_output = run_cmd._run_command(["git", "stash", "list"], cwd=project_path, timeout=30)
    logs = db.list_task_logs(task["id"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert status_code == 0
    assert status_output.strip() == ""
    assert stash_code == 0
    assert f"codepilot preflight stash before task #{task['id']}" in stash_output
    assert any(log["phase"] == "preflight" and "git stash pop" in log["output"] for log in logs)


def test_run_backlog_pass_finalize_exception_fails_without_requeue(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    _register_project_with_dirty_policy(tmp_path, "stop")
    task = db.create_task("demo", "pass then finalize fails", agent="dual", max_retries=3)

    def fake_round_loop(ctx):
        db.create_task_log(
            task_id=ctx.task["id"],
            agent="codex",
            phase="builder",
            output="builder exit 0",
            exit_code=0,
            started_at="2026-01-01T00:00:00",
            finished_at="2026-01-01T00:00:01",
            duration=1,
        )
        db.create_task_log(
            task_id=ctx.task["id"],
            agent="codex-review",
            phase="reviewer",
            output="all good\nVERDICT: PASS",
            exit_code=0,
            started_at="2026-01-01T00:00:01",
            finished_at="2026-01-01T00:00:02",
            duration=1,
        )
        return run_cmd._BuiltinLoopOutcome(
            status="pass",
            round_num=1,
            builder=run_cmd._PhaseOutcome(agent="codex", exit_code=0, output="builder exit 0"),
            reviewer=run_cmd._PhaseOutcome(
                agent="codex-review",
                exit_code=0,
                output="all good\nVERDICT: PASS",
            ),
            verdict="pass",
        )

    def fail_auto_commit(*args, **kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(run_cmd, "_run_builtin_round_loop", fake_round_loop)
    monkeypatch.setattr(run_cmd, "_git_auto_commit", fail_auto_commit)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True, quiet=True)
    current = db.get_task(task["id"])
    logs = db.list_task_logs(task["id"])

    assert stats["processed"] == 1
    assert stats["failed"] == 1
    assert stats["requeued"] == 0
    assert current["status"] == "failed"
    assert current["retry_count"] == 0
    assert "finalize/auto-commit" in (current["error_message"] or "")
    assert "Invalid argument" in (current["error_message"] or "")
    assert db.next_backlog_task("demo") == []
    assert any(log["phase"] == "builder" and log["exit_code"] == 0 for log in logs)
    assert any("VERDICT: PASS" in log["output"] for log in logs)


def test_run_backlog_pass_merge_finalize_failure_does_not_requeue(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    base_branch = _init_git_repo(project_path)
    config_file = project_path / "AGENTS.toml"
    config_file.write_text(
        f"""
[project]
name = "demo"
base_branch = "{base_branch}"

[automation]
per_task_branch = true
task_workspace = "branch"
""".strip(),
        encoding="utf-8",
    )
    run_cmd._run_command(["git", "add", "AGENTS.toml"], cwd=project_path, timeout=30)
    commit_code, commit_output = run_cmd._run_command(
        ["git", "commit", "-m", "add config"],
        cwd=project_path,
        timeout=120,
    )
    assert commit_code == 0, commit_output
    db.register_project("demo", str(project_path), base_branch=base_branch, config_file=str(config_file))
    task = db.create_task("demo", "pass then merge fails", agent="dual", max_retries=3)
    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=0,
            output="builder exit 0",
            review_output="VERDICT: PASS",
            summary="done",
            executor="builtin",
        ),
    )

    def fail_merge(*args, **kwargs):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(run_cmd, "_git_merge_task_branch", fail_merge)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)
    current = db.get_task(task["id"])

    assert stats["processed"] == 1
    assert stats["failed"] == 1
    assert stats["requeued"] == 0
    assert current["status"] == "failed"
    assert current["retry_count"] == 0
    assert "merge/finalize" in (current["error_message"] or "")
    assert "merge exploded" in (current["error_message"] or "")


def test_notify_task_event_routes_generic_progress_without_feishu_for_user_source(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    context = run_orchestrator_mod._RunContext(
        project={"name": "demo", "path": str(project_path)},
        project_path=project_path,
        config=None,
        base_branch="dev",
        shell_info=object(),
        executor="builtin",
        max_review_rounds=2,
        per_task_branch_enabled=False,
        task_workspace="branch",
    )
    generic_events: list[dict] = []
    feishu_events: list[dict] = []
    web_events: list[dict] = []
    monkeypatch.setattr(
        run_orchestrator_mod,
        "_publish_web_task_event",
        lambda *args, **kwargs: web_events.append(kwargs),
    )
    monkeypatch.setattr(run_cmd, "notify_task_event", lambda *args, **kwargs: generic_events.append(kwargs) or True)
    monkeypatch.setattr(
        run_cmd,
        "notify_feishu_task_event",
        lambda **kwargs: feishu_events.append(kwargs) or True,
    )

    run_orchestrator_mod._notify_task_event(
        context,
        {"id": 9, "title": "普通来源任务", "source": "user"},
        event="phase_start",
        phase="builder",
        message="开始构建",
        status="in_progress",
    )

    assert [item["event"] for item in generic_events] == ["phase_start"]
    assert [item["event"] for item in web_events] == ["phase_start"]
    assert feishu_events == []


def test_notify_task_event_routes_feishu_origin_to_source_chat(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    context = run_orchestrator_mod._RunContext(
        project={"name": "demo", "path": str(project_path)},
        project_path=project_path,
        config=None,
        base_branch="dev",
        shell_info=object(),
        executor="builtin",
        max_review_rounds=2,
        per_task_branch_enabled=False,
        task_workspace="branch",
    )
    generic_events: list[dict] = []
    feishu_events: list[dict] = []
    web_events: list[dict] = []
    monkeypatch.setattr(
        run_orchestrator_mod,
        "_publish_web_task_event",
        lambda *args, **kwargs: web_events.append(kwargs),
    )
    monkeypatch.setattr(run_cmd, "notify_task_event", lambda *args, **kwargs: generic_events.append(kwargs) or True)
    monkeypatch.setattr(
        run_cmd,
        "notify_feishu_task_event",
        lambda **kwargs: feishu_events.append(kwargs) or True,
    )

    run_orchestrator_mod._notify_task_event(
        context,
        {"id": 10, "title": "飞书来源任务", "source": "feishu:chat-source"},
        event="phase_end",
        phase="reviewer",
        message="Review 完成",
        status="in_progress",
    )

    assert [item["event"] for item in generic_events] == ["phase_end"]
    assert [item["event"] for item in web_events] == ["phase_end"]
    assert [item["chat_ids"] for item in feishu_events] == [["chat-source"]]


def test_progress_event_forwards_task_log_stream_without_external_status_notification(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    context = run_orchestrator_mod._RunContext(
        project={"name": "demo", "path": str(project_path)},
        project_path=project_path,
        config=None,
        base_branch="dev",
        shell_info=object(),
        executor="builtin",
        max_review_rounds=2,
        per_task_branch_enabled=False,
        task_workspace="branch",
    )
    web_progress_events: list[dict] = []
    external_events: list[dict] = []
    monkeypatch.setattr(
        run_orchestrator_mod,
        "_publish_web_progress_event",
        lambda event: web_progress_events.append(event),
    )
    monkeypatch.setattr(run_cmd, "notify_task_event", lambda *args, **kwargs: external_events.append(kwargs) or True)

    run_orchestrator_mod._notify_progress_event(
        context,
        {"id": 11, "title": "streaming task"},
        {
            "task_id": 11,
            "stage": "builder",
            "level": "info",
            "message": "",
            "extra": {
                "task_log_stream": True,
                "task_log_chunk": "hello",
                "task_log_start": 0,
                "task_log_end": 5,
            },
        },
    )

    assert len(web_progress_events) == 1
    assert web_progress_events[0]["extra"]["task_log_stream"] is True
    assert external_events == []


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


def test_prepare_task_workspace_resumes_dirty_existing_worktree(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    worktree_path = tmp_path / "task-worktree"
    project_path.mkdir()
    worktree_path.mkdir()
    task = {"id": 33, "title": "resume dirty worktree", "agent": "dual"}
    expected_branch = run_cmd._task_branch_name(task["id"], task["title"])
    context = run_orchestrator_mod._RunContext(
        project={"name": "demo", "path": str(project_path)},
        project_path=project_path,
        config=None,
        base_branch="dev",
        shell_info=object(),
        executor="builtin",
        max_review_rounds=2,
        per_task_branch_enabled=True,
        task_workspace="worktree",
    )

    monkeypatch.setattr(run_cmd, "_builtin_review_requires_git", lambda *args, **kwargs: False)
    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "")
    monkeypatch.setattr(run_cmd, "_handle_preflight_dirty_worktree", lambda *args, **kwargs: "")
    monkeypatch.setattr(run_cmd, "_builtin_base_branch_lock_error", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        run_cmd,
        "_git_prepare_task_worktree",
        lambda *args, **kwargs: (expected_branch, worktree_path),
    )
    monkeypatch.setattr(run_cmd, "_git_has_changes", lambda path: Path(path) == worktree_path)

    workspace = run_orchestrator_mod._prepare_task_workspace(
        context,
        task,
        auto_commit=True,
        dry_run=False,
    )

    assert workspace.preflight_error == ""
    assert workspace.task_branch == expected_branch
    assert workspace.execution_path == worktree_path
    assert workspace.resume_existing_task_branch is True


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
