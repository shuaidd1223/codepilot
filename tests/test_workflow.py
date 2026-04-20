from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.agent_support import ai_guide_markdown, command_manifest
from codepilot import binary as binary_mod
from codepilot import db
from codepilot import ai as ai_mod
from codepilot import runtime as runtime_mod
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.config import load_project_config


def _init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0


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


def test_reset_task_for_retry_restores_backlog_state(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me", max_retries=2)
    db.update_task(
        task["id"],
        status="failed",
        retry_count=2,
        branch_name="feature/retry",
        worktree_path=str(project_path / "worktree"),
        error_message="boom",
        delivery_record="old delivery",
        started_at="2026-04-12T10:00:00",
        completed_at="2026-04-12T10:10:00",
        run_phase="reviewer",
        heartbeat_at="2026-04-12T10:05:00",
        active_pid=12345,
        current_log_path=str(project_path / "task.log"),
        last_output="broken output",
        stop_requested=1,
        stop_reason="stop",
    )

    reset = db.reset_task_for_retry(task["id"])

    assert reset["status"] == "backlog"
    assert reset["retry_count"] == 0
    assert reset["branch_name"] is None
    assert reset["worktree_path"] is None
    assert reset["error_message"] is None
    assert reset["delivery_record"] is None
    assert reset["started_at"] is None
    assert reset["completed_at"] is None
    assert reset["run_phase"] is None
    assert reset["heartbeat_at"] is None
    assert reset["active_pid"] is None
    assert reset["current_log_path"] is None
    assert reset["last_output"] is None
    assert reset["stop_requested"] == 0
    assert reset["stop_reason"] is None


def test_retry_command_requeues_failed_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me", max_retries=2)
    db.update_task(task["id"], status="failed", retry_count=2, error_message="boom")

    runner = CliRunner()
    result = runner.invoke(main, ["retry", str(task["id"])])

    assert result.exit_code == 0
    current = db.get_task(task["id"])
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert current["error_message"] is None
    assert "已重新放回 backlog" in result.output
    assert "codepilot run -p demo" in result.output


def test_webui_dashboard_payload_lists_projects_and_tasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    first = db.create_task("demo", "task A", agent="codex", priority="P1")
    second = db.create_task("demo", "task B", agent="codex", priority="P2")
    db.update_task(second["id"], status="failed", error_message="boom")

    payload = webui_mod.dashboard_payload("demo")

    assert payload["selected_project"] == "demo"
    assert len(payload["projects"]) == 1
    assert payload["projects"][0]["stats"]["failed"] == 1
    assert [task["id"] for task in payload["tasks"]] == [first["id"], second["id"]]


def test_webui_retry_and_promote_actions_update_task_state(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "task A", agent="codex", priority="P2", max_retries=3)
    db.update_task(task["id"], status="failed", retry_count=2, error_message="boom")

    retried = webui_mod.retry_task_action(task["id"])
    current = db.get_task(task["id"])
    assert retried["ok"] is True
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0

    promoted = webui_mod.promote_task_action(task["id"])
    current = db.get_task(task["id"])
    assert promoted["ok"] is True
    assert current["status"] == "backlog"
    assert current["priority"] == "P0"


def test_webui_create_task_action_creates_task_for_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path), default_mode="codex")

    created = webui_mod.create_task_action(
        "demo",
        "从 Web UI 新建任务",
        content="补一段说明",
        priority="P1",
        agent="auto",
        max_retries=4,
    )

    assert created["ok"] is True
    task = db.get_task(created["task"]["id"])
    assert task["title"] == "从 Web UI 新建任务"
    assert task["content"] == "补一段说明"
    assert task["priority"] == "P1"
    assert task["agent"] == "codex"
    assert task["max_retries"] == 4


def test_webui_task_detail_payload_contains_log_and_content(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "task A", content="详细任务内容", agent="codex", priority="P2")
    log_path = project_path / "task.log"
    log_path.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    db.update_task(
        task["id"],
        status="in_progress",
        current_log_path=str(log_path),
        error_message="",
        depends_on=[3, 4],
    )

    detail = webui_mod.task_detail_payload(task["id"])

    assert detail["content"] == "详细任务内容"
    assert detail["depends_on"] == [3, 4]
    assert "line-3" in detail["log_text"]
    assert detail["current_log_path"] == str(log_path)


def test_webui_submit_requirement_action_records_job_and_tasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path), default_mode="codex")

    def fake_run_requirement_workflow(**kwargs):
        task = db.create_task(
            kwargs["project_info"]["name"],
            kwargs["title"],
            content="generated",
            agent=kwargs.get("task_agent") or "codex",
            priority=kwargs.get("priority") or "P2",
            project_path=kwargs["project_info"]["path"],
            max_retries=kwargs.get("max_retries") or 3,
        )
        return {
            "summary": "拆分完成",
            "tasks": [task],
            "run": {"done": 1, "failed": 0, "requeued": 0},
        }

    monkeypatch.setattr(webui_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    result = webui_mod.submit_requirement_action(
        "demo",
        "让 Web UI 直接接收需求",
        execute=True,
        planner="codex",
        agent="codex",
        run_async=False,
    )

    assert result["ok"] is True
    jobs = webui_mod.list_ui_jobs("demo")
    assert jobs
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["task_ids"]
    task = db.get_task(jobs[0]["task_ids"][0])
    assert task["title"] == "让 Web UI 直接接收需求"


def test_retry_command_rejects_running_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me")
    db.update_task(task["id"], status="in_progress", active_pid=12345)

    runner = CliRunner()
    result = runner.invoke(main, ["retry", str(task["id"])])

    assert result.exit_code == 0
    current = db.get_task(task["id"])
    assert current["status"] == "in_progress"
    assert "正在运行中" in result.output


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


def test_run_backlog_can_fail_without_requeue(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", max_retries=3)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder failed",
            review_output="VERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, retry_on_failure=False)
    current = db.get_task(task["id"])

    assert stats["failed"] == 1
    assert stats["requeued"] == 0
    assert current["status"] == "failed"
    assert current["retry_count"] == 1
    assert "review 未通过" in (current["error_message"] or "")


def test_resolve_project_for_prompt_uses_current_directory(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    project = auto_cmd.resolve_project_for_prompt()

    assert project["name"] == "demo"
    assert project["path"] == str(project_path)


def test_resolve_project_for_prompt_syncs_defaults_from_config(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"
base_branch = "main"
default_mode = "codex"

[automation]
planner = "codex"
executor = "builtin"
auto_execute = true
confirm_before_execute = false
auto_commit = false
max_tasks = 4
max_retries = 2
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path), default_mode="dual")
    monkeypatch.chdir(project_path)

    project = auto_cmd.resolve_project_for_prompt()

    assert project["name"] == "demo"
    assert project["default_mode"] == "codex"


def test_project_config_does_not_leak_from_workspace_when_project_has_no_agents_toml(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))

    auto_cfg = auto_cmd._project_config(db.get_project("demo"))
    run_cfg = run_cmd._project_config(db.get_project("demo"))

    assert auto_cfg is None
    assert run_cfg is None


def test_resolve_project_for_prompt_does_not_overwrite_parent_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "AGENTS.toml").write_text(
        """
[project]
name = "child-demo"
base_branch = "main"
default_mode = "codex"

[automation]
planner = "codex"
executor = "builtin"
auto_execute = true
confirm_before_execute = false
auto_commit = false
max_tasks = 4
max_retries = 2
""".strip(),
        encoding="utf-8",
    )

    db.register_project("parent-demo", str(parent), default_mode="dual")
    monkeypatch.chdir(child)

    project = auto_cmd.resolve_project_for_prompt()
    parent_project = db.get_project("parent-demo")

    assert project["name"] == "child-demo"
    assert project["path"] == str(child)
    assert parent_project["path"] == str(parent)


def test_get_current_project_task_stats_uses_current_directory(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    workdir = project_path / "src"
    workdir.mkdir(parents=True)

    db.register_project("demo", str(project_path))

    backlog = db.create_task("demo", "task backlog")
    in_progress = db.create_task("demo", "task running")
    done = db.create_task("demo", "task done")
    failed = db.create_task("demo", "task failed")
    cancelled = db.create_task("demo", "task cancelled")

    db.update_task(in_progress["id"], status="in_progress")
    db.update_task(done["id"], status="done")
    db.update_task(failed["id"], status="failed")
    db.update_task(cancelled["id"], status="cancelled")

    monkeypatch.chdir(workdir)

    stats = db.get_current_project_task_stats()

    assert stats == {
        "backlog": 1,
        "in_progress": 1,
        "done": 1,
        "failed": 1,
        "cancelled": 1,
        "total": 5,
    }
    assert db.get_current_project_stats() == stats


def test_get_current_project_task_stats_accepts_explicit_path(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    nested = project_path / "nested"
    nested.mkdir(parents=True)

    db.register_project("demo", str(project_path))
    db.create_task("demo", "task backlog")
    done = db.create_task("demo", "task done")
    db.update_task(done["id"], status="done")

    stats = db.get_current_project_task_stats(nested)

    assert stats == {
        "backlog": 1,
        "in_progress": 0,
        "done": 1,
        "failed": 0,
        "cancelled": 0,
        "total": 2,
    }
    assert db.get_task_stats_by_path(nested) == stats
    assert db.get_current_project_stats(nested) == stats


def test_get_current_project_task_stats_returns_none_when_project_is_unregistered(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert db.get_current_project_task_stats() is None
    assert db.get_current_project_stats() is None
    assert db.get_current_project_task_stats(outside) is None
    assert db.get_task_stats_by_path(outside) is None


def test_get_current_project_task_stats_prefers_deepest_registered_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)

    db.register_project("parent-demo", str(parent))
    db.register_project("child-demo", str(child))

    db.create_task("parent-demo", "parent backlog")
    parent_done = db.create_task("parent-demo", "parent done")
    db.update_task(parent_done["id"], status="done")

    child_running = db.create_task("child-demo", "child running")
    child_failed = db.create_task("child-demo", "child failed")
    db.update_task(child_running["id"], status="in_progress")
    db.update_task(child_failed["id"], status="failed")

    monkeypatch.chdir(child)

    stats = db.get_current_project_task_stats()

    assert stats == {
        "backlog": 0,
        "in_progress": 1,
        "done": 0,
        "failed": 1,
        "cancelled": 0,
        "total": 2,
    }


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


def test_root_command_passes_selected_task_agent(tmp_path, monkeypatch):
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
    result = runner.invoke(main, ["--agent", "codex", "--no-execute", "实现一个自动重试机制"])

    assert result.exit_code == 0
    assert captured["task_agent"] == "codex"


def test_run_requirement_workflow_executes_without_retry_requeue(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    project = db.get_project("demo")
    captured = {}

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: {
            "summary": "ok",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                }
            ],
        },
    )
    monkeypatch.setattr(auto_cmd, "render_project_dashboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        auto_cmd,
        "run_backlog",
        lambda *args, **kwargs: captured.update(kwargs) or {"processed": 1, "done": 1, "failed": 0, "requeued": 0},
    )

    auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让它直接执行",
        planner="codex",
        execute=True,
        executor="builtin",
        auto_commit=False,
    )

    assert captured["retry_on_failure"] is False


def test_batch_add_with_default_agent_does_not_require_click_context(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.txt"
    tasks_file.write_text("任务一\n任务二\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file), "--no-ai"])

    assert result.exit_code == 0
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 2
    assert all(task["agent"] == "codex" for task in tasks)


def test_add_command_preserves_utf8_title_and_content_round_trip(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    title = "修复任务标题/内容在 Windows 控制台显示乱码"
    content = "# 任务说明\n\n1. 标题需要原样保留\n2. 内容也要原样保留"

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))
    monkeypatch.setattr(add_cmd, "generate_task_content", lambda *args, **kwargs: content)

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-t", title, "-a", "codex"])

    assert result.exit_code == 0
    created = db.list_tasks(project="demo")
    assert len(created) == 1
    assert created[0]["title"] == title
    assert created[0]["content"] == content


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

    # Pin intent to "task" so the CliRunner's stdin (which on Windows may
    # transcode Chinese through cp1252) can't send us down a different branch.
    monkeypatch.setattr(auto_cmd, "classify_intent", lambda text, **kw: {
        "intent": "task", "source": "forced",
    })
    # Skip multi-turn clarification — this test exercises the straight-to-plan path.
    monkeypatch.setattr(auto_cmd, "clarify_requirement", lambda title, **kw: {
        "status": "ready", "refined_title": title, "qa_history": [],
    })
    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="做一个自动重试机制\n/exit\n")

    assert result.exit_code == 0
    assert len(captured) == 1, f"expected one planner call, got {captured!r}"
    # The encoded title may lose bytes through CliRunner on Windows, but the
    # planner must at least have been invoked with a non-empty title.
    assert captured[0].strip()
    assert "CodePilot Chat" in result.output


def test_chat_command_reports_natural_language_error(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    def fake_run_requirement_workflow(**kwargs):
        raise RuntimeError("当前无法使用 Claude CLI，因为本机没有找到 claude 命令。")

    monkeypatch.setattr(auto_cmd, "classify_intent", lambda text, **kw: {
        "intent": "task", "source": "forced",
    })
    monkeypatch.setattr(auto_cmd, "clarify_requirement", lambda title, **kw: {
        "status": "ready", "refined_title": title, "qa_history": [],
    })
    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="做一个自动重试机制\n/exit\n")

    assert result.exit_code == 0
    assert "当前无法使用 Claude CLI" in result.output
    assert "Traceback" not in result.output


def test_chat_help_mentions_stats_command():
    assert "/stats" in auto_cmd._chat_help()


def test_chat_status_renders_dashboard(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    called = {}

    monkeypatch.setattr(auto_cmd, "render_project_dashboard", lambda *args, **kwargs: called.update({"args": args, "kwargs": kwargs}))

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="/status\n/exit\n")

    assert result.exit_code == 0
    assert called["args"][0] == "demo"
    assert "demo" in called["kwargs"]["title"]


def test_chat_stats_outputs_status_summary(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    db.create_task("demo", "task backlog")
    running = db.create_task("demo", "task running")
    done = db.create_task("demo", "task done")
    failed = db.create_task("demo", "task failed")
    cancelled = db.create_task("demo", "task cancelled")

    db.update_task(running["id"], status="in_progress")
    db.update_task(done["id"], status="done")
    db.update_task(failed["id"], status="failed")
    db.update_task(cancelled["id"], status="cancelled")

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="/stats\n/exit\n")

    assert result.exit_code == 0
    assert "状态统计  demo" in result.output
    assert "进行中:1" in result.output
    assert "待办:1" in result.output
    assert "失败:1" in result.output
    assert "已取消:1" in result.output
    assert "完成:1" in result.output
    assert "总计:5" in result.output


def test_go_command_wraps_runtime_error_as_click_exception(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    def fake_run_requirement_workflow(**kwargs):
        raise RuntimeError("当前无法使用 Claude CLI，因为本机没有找到 claude 命令。")

    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["让工具自己优化自己"])

    assert result.exit_code != 0
    assert "当前无法使用 Claude CLI" in result.output
    assert "Traceback" not in result.output


def test_generate_task_breakdown_uses_codex_planner(monkeypatch):
    captured = {}

    def fake_run_codex_schema_prompt(prompt, schema, *, project_path="", config_ref=None, timeout=240):
        captured["project_path"] = project_path
        captured["config_ref"] = config_ref
        captured["timeout"] = timeout
        return {
            "summary": "ok",
            "complexity": "simple",
            "should_split": False,
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
                }
            ],
        }

    monkeypatch.setattr("codepilot.ai._run_codex_schema_prompt", fake_run_codex_schema_prompt)

    breakdown = auto_cmd.generate_task_breakdown(
        title="实现一个自动重试机制",
        project_path="D:/demo",
        planner="codex",
        max_tasks=3,
    )

    assert breakdown["tasks"][0]["title"] == "step 1"
    assert captured["project_path"] == "D:/demo"
    assert captured["config_ref"] is None


def test_run_codex_schema_prompt_kills_process_and_raises_runtime_error_on_interrupt(monkeypatch):
    class _FakeProvider:
        name = "Codex"

        def find_executable(self):
            return Path("codex")

    class _FakeStdin:
        def write(self, _text):
            return None

        def close(self):
            return None

    class _FakeStream:
        def read(self):
            return ""

        def __iter__(self):
            return iter(())

    class _FakeProcess:
        def __init__(self):
            self.pid = 4321
            self.stdin = _FakeStdin()
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode = None

        def poll(self):
            raise KeyboardInterrupt()

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

    fake_process = _FakeProcess()
    killed: list[int] = []

    monkeypatch.setattr(ai_mod, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(ai_mod, "resolve_cli_provider", lambda *args, **kwargs: _FakeProvider())
    monkeypatch.setattr(ai_mod.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(ai_mod, "_kill_process_tree", lambda pid: killed.append(int(pid)))

    try:
        ai_mod._run_codex_schema_prompt("prompt", {"type": "object"})
    except RuntimeError as exc:
        assert "中断" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert killed == [4321]
    assert fake_process.returncode == -9


def test_run_claude_schema_prompt_kills_process_and_raises_runtime_error_on_interrupt(monkeypatch):
    class _FakeProvider:
        name = "Claude CLI"

        def find_executable(self):
            return Path("claude")

    class _FakeStream:
        def read(self):
            return ""

        def __iter__(self):
            return iter(())

    class _FakeProcess:
        def __init__(self):
            self.pid = 8765
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode = None

        def poll(self):
            raise KeyboardInterrupt()

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

    fake_process = _FakeProcess()
    killed: list[int] = []

    monkeypatch.setattr(ai_mod, "resolve_cli_provider", lambda *args, **kwargs: _FakeProvider())
    monkeypatch.setattr(ai_mod.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(ai_mod, "_kill_process_tree", lambda pid: killed.append(int(pid)))

    try:
        ai_mod._run_claude_schema_prompt("prompt", {"type": "object"}, planner="claude")
    except RuntimeError as exc:
        assert "中断" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert killed == [8765]
    assert fake_process.returncode == -9


def test_run_requirement_workflow_falls_back_to_single_codex_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Codex planning timeout")),
    )

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让工具自己优化自己",
        planner="codex",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert payload["complexity"] == "simple"
    assert payload["should_split"] is False
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["agent"] == "codex"


def test_run_requirement_workflow_uses_registered_config_file_for_provider_resolution(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    config_root = tmp_path / "config-root"
    project_path.mkdir()
    config_root.mkdir()
    config_file = config_root / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
default_mode = "codex"
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path), default_mode="codex", config_file=str(config_file))
    project = db.get_project("demo")
    captured = {}

    def fake_check_provider(agent, project_path=None):
        captured["provider_path"] = project_path
        return True, f"ok:{agent}"

    monkeypatch.setattr(auto_cmd, "check_provider_availability", fake_check_provider)
    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: captured.update({"config_ref": kwargs.get("config_ref")}) or {
            "summary": "ok",
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                }
            ],
        },
    )

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让工具自己优化自己",
        planner="codex",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert payload["tasks"][0]["agent"] == "codex"
    assert captured["provider_path"] == str(config_file)
    assert captured["config_ref"] == str(config_file)


def test_run_requirement_workflow_does_not_fallback_on_non_timeout_codex_error(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("当前无法使用 Codex，因为本机没有找到 `codex` 命令。")),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        try:
            auto_cmd.run_requirement_workflow(
                project_info=project,
                title="让工具自己优化自己",
                planner="codex",
                execute=False,
                executor="builtin",
                auto_commit=False,
            )
        except Exception as exc:
            assert isinstance(exc, click.ClickException)
            assert "当前无法使用 Codex" in exc.format_message()
        else:
            raise AssertionError("expected ClickException")


def test_normalize_agent_name_preserves_dual():
    assert ai_mod.normalize_agent_name("dual") == "dual"


def test_generate_task_content_uses_codex_for_dual(monkeypatch):
    captured = {}

    monkeypatch.setattr(ai_mod, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(ai_mod, "_collect_project_context", lambda project_path: "")

    def fake_run_cli_provider(provider, prompt, env_overrides=None):
        captured["provider"] = provider.name
        captured["prompt"] = prompt
        return "generated"

    monkeypatch.setattr(ai_mod, "_run_cli_provider", fake_run_cli_provider)

    content = ai_mod.generate_task_content("实现一个自动重试机制", agent="dual")

    assert content == "generated"
    assert captured["provider"] == "OpenAI Codex"
    assert "实现一个自动重试机制" in captured["prompt"]


def test_resolve_task_agent_preserves_dual(monkeypatch):
    monkeypatch.setattr(auto_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(auto_cmd, "_project_config", lambda project_info: None)

    resolved = auto_cmd._resolve_task_agent({"default_mode": "dual", "path": "D:/demo"}, "dual", "builtin")

    assert resolved == "dual"


def test_resolve_builtin_phase_agent_uses_dual_split():
    assert run_cmd._resolve_builtin_phase_agent("dual", "builder") == ("codex", None)
    assert run_cmd._resolve_builtin_phase_agent("dual", "reviewer") == ("claude", None)


def test_resolve_builtin_phase_agent_uses_configured_dual_split(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = "sonnet"
reviewer = "codex"
""".strip(),
        encoding="utf-8",
    )

    assert run_cmd._resolve_builtin_phase_agent("dual", "builder", project_ref=project_path) == ("claude", "sonnet")
    assert run_cmd._resolve_builtin_phase_agent("dual", "reviewer", project_ref=project_path) == ("codex", None)


def test_agents_config_reads_and_normalizes_dual_phase_agents(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = " codex "
reviewer = " sonnet "
""".strip(),
        encoding="utf-8",
    )

    cfg = load_project_config(project_path)

    assert cfg is not None
    assert cfg.builder == "codex"
    assert cfg.reviewer == "claude-sonnet"


def test_agents_config_treats_blank_dual_phase_agents_as_unset(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = "   "
reviewer = ""
codex_cmd = "codex"
claude_cmd = "claude"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_project_config(project_path)

    assert cfg is not None
    assert cfg.builder is None
    assert cfg.reviewer is None
    assert cfg.codex_cmd == "codex"
    assert cfg.claude_cmd == "claude"


def test_task_branch_name_uses_slug_and_fallback():
    assert run_cmd._task_branch_name(20, "Add API endpoint") == "feat/task-20-add-api-endpoint"
    assert run_cmd._task_branch_name(21, "实现中文能力") == "feat/task-21-task"


def test_task_worktree_path_uses_configured_relative_base(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": "var/worktrees",
    }

    resolved_base = run_cmd._resolve_project_worktree_base(project_info)
    worktree_path = run_cmd._task_worktree_path(project_info, task_id=7, title="Add API endpoint")

    assert resolved_base == (project_path / "var" / "worktrees").resolve()
    assert worktree_path == resolved_base / "task-7-add-api-endpoint"


def test_git_prepare_and_cleanup_task_worktree(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=7, title="Add API endpoint")

    branch_name, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=7,
        title="Add API endpoint",
        base_branch=base_branch,
        worktree_path=expected_path,
    )

    assert branch_name == run_cmd._task_branch_name(7, "Add API endpoint")
    assert worktree_path == expected_path.resolve()
    assert worktree_path.exists()
    assert run_cmd._git_worktree_exists(project_path, worktree_path) is True

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=worktree_path, timeout=30)
    assert code == 0
    assert output.strip() == branch_name

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=branch_name)

    assert worktree_path.exists() is False
    assert run_cmd._git_worktree_exists(project_path, worktree_path) is False

    code, output = run_cmd._run_command(["git", "branch", "--list", branch_name], cwd=project_path, timeout=30)
    assert code == 0
    assert branch_name not in output


def test_git_prepare_task_worktree_allows_existing_empty_dir(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=8, title="Existing empty dir")
    expected_path.mkdir(parents=True)

    branch_name, worktree_path = run_cmd._git_prepare_task_worktree(
        project_path,
        task_id=8,
        title="Existing empty dir",
        base_branch=base_branch,
        worktree_path=expected_path,
    )

    assert branch_name == run_cmd._task_branch_name(8, "Existing empty dir")
    assert worktree_path == expected_path.resolve()
    assert worktree_path.exists()
    assert run_cmd._git_worktree_exists(project_path, worktree_path) is True

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch=branch_name)
    assert worktree_path.exists() is False


def test_git_prepare_task_worktree_refuses_other_branch_on_same_path(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "worktree_base": str(worktree_base),
    }
    expected_path = run_cmd._task_worktree_path(project_info, task_id=7, title="Add API endpoint")
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/other", str(expected_path), base_branch],
        cwd=project_path,
        capture_output=True,
        check=True,
    )

    with pytest.raises(RuntimeError, match="已被其他分支占用"):
        run_cmd._git_prepare_task_worktree(
            project_path,
            task_id=7,
            title="Add API endpoint",
            base_branch=base_branch,
            worktree_path=expected_path,
        )

    assert run_cmd._git_worktree_exists(project_path, expected_path) is True
    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=expected_path, timeout=30)
    assert code == 0
    assert output.strip() == "feat/other"

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=expected_path, task_branch="feat/other")


def test_git_prepare_task_worktree_refuses_same_task_branch_on_other_worktree(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    task_branch = run_cmd._task_branch_name(7, "Add API endpoint")
    occupied_path = worktree_base / "occupied"
    target_path = worktree_base / "target"
    subprocess.run(
        ["git", "worktree", "add", "-b", task_branch, str(occupied_path), base_branch],
        cwd=project_path,
        capture_output=True,
        check=True,
    )

    with pytest.raises(RuntimeError, match="任务分支已被其他 worktree 占用"):
        run_cmd._git_prepare_task_worktree(
            project_path,
            task_id=7,
            title="Add API endpoint",
            base_branch=base_branch,
            worktree_path=target_path,
        )

    assert run_cmd._git_worktree_exists(project_path, occupied_path) is True
    assert run_cmd._git_worktree_exists(project_path, target_path) is False
    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=occupied_path, task_branch=task_branch)


def test_git_cleanup_task_worktree_refuses_branch_mismatch(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    worktree_path = worktree_base / "task-7-add-api-endpoint"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/other", str(worktree_path), base_branch],
        cwd=project_path,
        capture_output=True,
        check=True,
    )

    with pytest.raises(RuntimeError, match="拒绝移除不属于任务分支"):
        run_cmd._git_cleanup_task_worktree(
            project_path,
            worktree_path=worktree_path,
            task_branch=run_cmd._task_branch_name(7, "Add API endpoint"),
        )

    assert run_cmd._git_worktree_exists(project_path, worktree_path) is True
    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=worktree_path, timeout=30)
    assert code == 0
    assert output.strip() == "feat/other"

    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=worktree_path, task_branch="feat/other")


def test_git_cleanup_task_worktree_does_not_delete_branch_without_matching_worktree(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    task_branch = run_cmd._task_branch_name(7, "Add API endpoint")
    subprocess.run(["git", "branch", task_branch], cwd=project_path, capture_output=True, check=True)

    missing_path = tmp_path / "missing-worktree"
    run_cmd._git_cleanup_task_worktree(project_path, worktree_path=missing_path, task_branch=task_branch)

    code, output = run_cmd._run_command(["git", "branch", "--list", task_branch], cwd=project_path, timeout=30)
    assert code == 0
    assert task_branch in output


def test_run_backlog_creates_task_branch_and_checks_out_base_after_merge(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "Add API endpoint", agent="dual", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin"),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])
    expected_branch = run_cmd._task_branch_name(task["id"], task["title"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert current["branch_name"] == expected_branch

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == base_branch

    code, output = run_cmd._run_command(["git", "branch", "--list", expected_branch], cwd=project_path, timeout=30)
    assert code == 0
    assert expected_branch not in output, "task branch should be deleted after merge"


def test_run_backlog_requeues_when_merge_back_fails_with_uncommitted_changes(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)

    base_branch = run_cmd._git_current_branch(project_path)
    db.register_project("demo", str(project_path), base_branch=base_branch)
    task = db.create_task("demo", "leave dirty file", agent="dual", max_retries=3)

    def _fake_executor(*args, **kwargs):
        (project_path / "dirty.txt").write_text("left dirty", encoding="utf-8")
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", _fake_executor)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    expected_branch = run_cmd._task_branch_name(task["id"], task["title"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1
    assert "回合并失败" in (current["error_message"] or "")

    code, output = run_cmd._run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    assert code == 0
    assert output.strip() == expected_branch


def test_run_builtin_phase_codex_review_omits_prompt(monkeypatch, tmp_path):
    captured = {}

    monkeypatch.setattr(run_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))

    class DummyProvider:
        name = "OpenAI Codex"

        @staticmethod
        def find_executable():
            return Path("C:/fake/codex.CMD")

    monkeypatch.setattr(run_cmd, "resolve_cli_provider", lambda provider_key, project_path=None: DummyProvider())

    def fake_run_command(cmd, **kwargs):
        captured["cmd"] = cmd
        return 0, ""

    monkeypatch.setattr(run_cmd, "_run_command", fake_run_command)
    monkeypatch.setattr(run_cmd, "_read_output_file", lambda path: "")

    label, exit_code, output = run_cmd._run_builtin_phase(
        task={"agent": "codex"},
        project_path=tmp_path,
        phase="reviewer",
        prompt="请做审查",
        output_path=tmp_path / "review.txt",
        timeout=30,
    )

    assert label == "codex-review"
    assert exit_code == 0
    assert output == ""
    assert "--uncommitted" in captured["cmd"]
    assert "--ephemeral" in captured["cmd"]
    assert "请做审查" not in captured["cmd"]


def test_extract_review_verdict_uses_codex_review_markers():
    fail_output = """
The change breaks behavior.

Review comment:

- [P1] Keep add returning a sum
""".strip()

    pass_output = "The only change adds a comment and does not affect behavior."

    assert run_cmd._extract_review_verdict(fail_output, "codex-review") == "fail"
    assert run_cmd._extract_review_verdict(pass_output, "codex-review") == "pass"
    assert run_cmd._extract_review_verdict("**VERDICT: FAIL**", "claude-review") == "fail"


def test_run_builtin_executor_fails_when_review_verdict_is_unknown(monkeypatch, tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    task_file = project_path / "task.md"
    task_file.write_text("demo", encoding="utf-8")

    phases = iter(
        [
            ("codex", 0, "builder ok"),
            ("claude-review", 0, "没有输出 verdict"),
        ]
    )

    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "")
    monkeypatch.setattr(run_cmd, "_run_builtin_phase", lambda **kwargs: next(phases))
    monkeypatch.setattr(run_cmd, "_write_task_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_cmd, "_git_auto_commit", lambda *args, **kwargs: "deadbee")

    result = run_cmd._run_builtin_executor(
        {"id": 7, "title": "demo", "agent": "dual"},
        {"path": str(project_path)},
        task_file,
        auto_commit=False,
        max_review_rounds=1,
    )

    assert result.exit_code == 2
    assert "review 结果不明确" in (result.summary or "")


def test_builtin_runtime_dir_is_outside_project(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()

    runtime_dir = run_cmd._builtin_runtime_dir({"name": "demo", "path": str(project_path)})

    assert runtime_dir.exists()
    assert not runtime_dir.is_relative_to(project_path)
    assert ".codepilot" in str(runtime_dir)


def test_run_backlog_builtin_dirty_workspace_requeues_without_retry(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    (project_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "blocked by dirty tree", max_retries=2)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert "未提交改动" in (current["error_message"] or "")


def test_generate_task_content_uses_project_configured_codex_cmd(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake_codex = tmp_path / "tools" / "codex.cmd"
    fake_codex.parent.mkdir()
    fake_codex.write_text("@echo off\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        f"""
[project]
name = "demo"

[agents]
codex_cmd = "{fake_codex.as_posix()}"
""".strip(),
        encoding="utf-8",
    )

    captured = {}
    monkeypatch.setattr(ai_mod, "_collect_project_context", lambda project_path: "")

    def fake_run_cli_provider(provider, prompt, env_overrides=None):
        captured["provider_cmd"] = provider.cmd
        captured["prompt"] = prompt
        return "generated"

    monkeypatch.setattr(ai_mod, "_run_cli_provider", fake_run_cli_provider)

    content = ai_mod.generate_task_content(
        "实现一个自动重试机制",
        project_path=str(project_path),
        agent="codex",
    )

    assert content == "generated"
    assert captured["provider_cmd"] == fake_codex.as_posix()
    assert "实现一个自动重试机制" in captured["prompt"]


def test_run_builtin_phase_uses_project_configured_codex_cmd(monkeypatch, tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake_codex = tmp_path / "tools" / "codex.cmd"
    fake_codex.parent.mkdir()
    fake_codex.write_text("@echo off\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        f"""
[project]
name = "demo"

[agents]
codex_cmd = "{fake_codex.as_posix()}"
""".strip(),
        encoding="utf-8",
    )

    captured = {}
    monkeypatch.setattr(run_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))

    def fake_run_command(cmd, **kwargs):
        captured["cmd"] = cmd
        return 0, ""

    monkeypatch.setattr(run_cmd, "_run_command", fake_run_command)
    monkeypatch.setattr(run_cmd, "_read_output_file", lambda path: "")

    label, exit_code, output = run_cmd._run_builtin_phase(
        task={"agent": "codex"},
        project_path=project_path,
        phase="builder",
        prompt="请实现功能",
        output_path=project_path / "builder.txt",
        timeout=30,
    )

    assert label == "codex"
    assert exit_code == 0
    assert output == ""
    assert Path(captured["cmd"][0]) == fake_codex
    assert "--skip-git-repo-check" in captured["cmd"]
    assert "--ephemeral" in captured["cmd"]


def test_run_backlog_builtin_non_git_repo_requeues_without_retry(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "needs git first", max_retries=2)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert "Git 仓库" in (current["error_message"] or "")


def test_run_backlog_builtin_codex_review_requires_git_even_without_auto_commit(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "needs git for review", agent="codex", max_retries=2)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert "Codex review" in (current["error_message"] or "")


def test_run_backlog_builtin_dual_can_proceed_without_git_when_auto_commit_disabled(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    db.create_task("demo", "dual task", agent="dual", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=0, output="ok", executor="builtin"),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)

    assert stats["done"] == 1


def test_run_backlog_builtin_dual_with_codex_reviewer_requires_git(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = "claude"
reviewer = "codex"
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "dual task", agent="dual", max_retries=2)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert "Codex review" in (current["error_message"] or "")


def test_run_backlog_marks_task_cancelled_when_executor_is_stopped(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "cancel me", agent="dual", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: (_ for _ in ()).throw(run_cmd.TaskCancelled("手动停止")),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["cancelled"] == 1
    assert current["status"] == "cancelled"
    assert current["error_message"] == "手动停止"


def test_reap_stalled_tasks_marks_dead_in_progress_task_failed(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stuck task", agent="codex")

    db.update_task(
        task["id"],
        status="in_progress",
        started_at="2026-01-01T00:00:00",
        heartbeat_at="2026-01-01T00:00:00",
        active_pid=None,
        run_phase="builder",
    )

    reaped = runtime_mod.reap_stalled_tasks("demo", stale_after_seconds=1)
    current = db.get_task(task["id"])

    assert len(reaped) == 1
    assert current["status"] == "failed"
    assert "心跳已超过" in (current["error_message"] or "")


def test_reap_stalled_tasks_cleans_dead_process_tree_before_marking_failed(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stale task", agent="codex")

    db.update_task(
        task["id"],
        status="in_progress",
        started_at="2026-01-01T00:00:00",
        heartbeat_at="2026-01-01T00:00:00",
        active_pid=123456,
        run_phase="builder",
    )

    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: False)
    killed: list[int] = []
    monkeypatch.setattr(runtime_mod, "stop_process_tree", lambda pid, wait_seconds=5: killed.append(int(pid)) or True)

    reaped = runtime_mod.reap_stalled_tasks("demo", stale_after_seconds=1)
    current = db.get_task(task["id"])

    assert len(reaped) == 1
    assert killed == [123456]
    assert current["status"] == "failed"


def test_stop_process_tree_windows_kills_descendants_even_if_root_is_gone(monkeypatch):
    import subprocess

    monkeypatch.setattr(runtime_mod.platform, "system", lambda: "Windows")

    processes = {
        200: {"parent": 100, "name": "codex.exe"},
        201: {"parent": 200, "name": "node.exe"},
    }

    def _descendants(root_pid: int) -> set[int]:
        found = set()
        while True:
            added = {pid for pid, meta in processes.items() if meta["parent"] in ({root_pid} | found)}
            if added.issubset(found):
                break
            found |= added
        return found

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0].lower() == "powershell.exe":
            payload = [
                {"ProcessId": pid, "ParentProcessId": meta["parent"], "Name": meta["name"]}
                for pid, meta in sorted(processes.items())
            ]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[0].lower() == "taskkill":
            target = int(cmd[cmd.index("/PID") + 1])
            # Simulate "root is gone": taskkill returns failure and does not cascade
            if target not in processes:
                return subprocess.CompletedProcess(cmd, 128, "", "not found")
            if "/T" in cmd:
                targets = _descendants(target) | {target}
            else:
                targets = {target}
            for pid in targets:
                processes.pop(pid, None)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: int(pid) in processes if pid else False)

    tick = {"t": 0.0}

    def fake_monotonic():
        tick["t"] += 0.4
        return tick["t"]

    monkeypatch.setattr(runtime_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(runtime_mod.time, "sleep", lambda _: None)

    assert runtime_mod.stop_process_tree(100, wait_seconds=1) is True
    assert processes == {}
    assert any(cmd[:4] == ["taskkill", "/PID", "200", "/T"] for cmd in calls)
    assert any(cmd[:4] == ["taskkill", "/PID", "201", "/T"] for cmd in calls)


def test_stop_process_tree_windows_falls_back_to_stop_process_when_taskkill_denied(monkeypatch):
    import subprocess

    monkeypatch.setattr(runtime_mod.platform, "system", lambda: "Windows")

    alive = {61432: True}
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0].lower() == "taskkill":
            return subprocess.CompletedProcess(cmd, 1, "", "Access is denied.")
        if cmd[:2] == ["powershell.exe", "-Command"] and "Stop-Process -Id 61432 -Force" in cmd[2]:
            alive[61432] = False
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: alive.get(int(pid), False) if pid else False)

    runtime_mod._windows_kill_pid(61432)

    assert alive[61432] is False
    assert any(cmd[0].lower() == "taskkill" for cmd in calls)
    assert any(cmd[:2] == ["powershell.exe", "-Command"] for cmd in calls)


def test_run_command_live_cleans_process_tree_on_unexpected_exception(tmp_path, monkeypatch):
    import sys

    log_path = tmp_path / "live.log"
    monkeypatch.setattr(run_cmd, "update_task_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_cmd, "get_stop_request", lambda task_id: (_ for _ in ()).throw(RuntimeError("boom")))

    killed: list[int] = []

    def _tracking_stop(pid):
        killed.append(int(pid))
        return runtime_mod.stop_process_tree(pid)

    monkeypatch.setattr(run_cmd, "stop_process_tree", _tracking_stop)

    try:
        run_cmd._run_command_live(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            task_id=1,
            phase="builder",
            log_path=log_path,
            timeout=30,
        )
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected RuntimeError")

    assert killed
    assert not runtime_mod.is_process_alive(killed[0])


def test_stop_command_cancels_in_progress_task_without_live_process(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stop me", agent="codex")
    db.update_task(task["id"], status="in_progress", active_pid=999999, run_phase="builder")

    monkeypatch.setattr("codepilot.commands.tasks.is_process_alive", lambda pid: False)
    monkeypatch.setattr("codepilot.commands.tasks.stop_process_tree", lambda pid: True)

    runner = CliRunner()
    result = runner.invoke(main, ["stop", str(task["id"])])
    current = db.get_task(task["id"])

    assert result.exit_code == 0
    assert "已停止" in result.output
    assert current["status"] == "cancelled"


def test_logs_command_reads_live_runtime_log(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "show logs", agent="codex")
    log_path = tmp_path / "task.log"
    log_path.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    db.update_task(task["id"], status="in_progress", current_log_path=str(log_path))

    runner = CliRunner()
    result = runner.invoke(main, ["logs", str(task["id"]), "--tail", "2"])

    assert result.exit_code == 0
    assert "line 2" in result.output
    assert "line 3" in result.output


def test_status_verbose_shows_runtime_summary_for_in_progress_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "visible task", agent="codex")
    db.update_task(
        task["id"],
        status="in_progress",
        run_phase="builder",
        heartbeat_at="2999-01-01T00:00:00",
        active_pid=None,
        last_output="running tests",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["status", "-p", "demo", "-v"])

    assert result.exit_code == 0
    assert "builder" in result.output
    assert "running tests" in result.output


def test_extract_error_hint_humanizes_json_payload():
    raw = """{"type":"result","subtype":"success","is_error":true,"result":"You've hit your limit · resets Apr 14, 1pm (Asia/Shanghai)"}"""

    hint = ai_mod._extract_error_hint(raw)

    assert "当前账号额度已用完" in hint
    assert "重置时间 Apr 14, 1pm" in hint
    assert "{" not in hint


def test_root_command_without_args_shows_help_in_non_interactive_mode():
    runner = CliRunner()
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_root_help_includes_ui_command():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "ui" in result.output


def test_ai_manifest_command_outputs_machine_readable_json():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "manifest"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["name"] == "CodePilot"
    assert any(command["name"] == "status" for command in payload["commands"])
    assert any(item["command"] == f"{payload['command_name']} ai manifest" for item in payload["structured_outputs"])


def test_ai_manifest_command_allows_version_and_command_override():
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ai", "manifest", "--version", "9.9.9", "--command-name", "mypilot", "--binary-name", "mypilot"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    release_bundle = next(item for item in payload["commands"] if item["name"] == "release_bundle")
    assert payload["version"] == "9.9.9"
    assert payload["command_name"] == "mypilot"
    assert payload["structured_outputs"][0]["command"] == "mypilot ai manifest"
    assert payload["commands"][0]["syntax"] == "mypilot init <path>"
    assert "dist/binary/linux-x86_64/mypilot" in release_bundle["examples"][1]


def test_repo_ai_manifest_file_stays_in_sync():
    manifest_path = Path(__file__).resolve().parents[1] / "AI_MANIFEST.json"

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert payload == command_manifest()


def test_repo_ai_usage_file_stays_in_sync():
    guide_path = Path(__file__).resolve().parents[1] / "AI_USAGE.zh-CN.md"

    assert guide_path.read_text(encoding="utf-8") == ai_guide_markdown()


def test_ai_guide_command_outputs_markdown_usage():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "guide"])

    assert result.exit_code == 0
    assert "# CodePilot AI 调用手册" in result.output
    assert "codepilot status -p <项目名> --json" in result.output
    assert "codepilot ai manifest" in result.output


def test_ai_prompt_command_outputs_short_agent_prompt():
    runner = CliRunner()
    result = runner.invoke(main, ["ai", "prompt"])

    assert result.exit_code == 0
    assert "codepilot \"需求文本\"" in result.output
    assert "codepilot release prepare --version <版本号>" in result.output


def test_find_json_accepts_options_after_keyword(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    db.create_task("demo", "needle task", content="contains needle")

    runner = CliRunner()
    result = runner.invoke(main, ["find", "needle", "-p", "demo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["tasks"][0]["title"] == "needle task"


def test_run_command_renders_plain_text_without_markup():
    runner = CliRunner()
    result = runner.invoke(main, ["run"])

    assert result.exit_code == 0
    assert "错误: 必须指定 --project" in result.output
    assert "[red]" not in result.output


def test_binary_default_install_dir_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    install_dir = binary_mod.default_install_dir()

    assert install_dir == (tmp_path / "LocalAppData" / "Programs" / "CodePilot" / "bin").resolve()


def test_update_project_version_updates_pyproject_and_init(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    init_file = package_dir / "__init__.py"
    pyproject.write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    init_file.write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    previous, current = binary_mod.update_project_version(tmp_path, "0.2.0")

    assert previous == "0.1.0"
    assert current == "0.2.0"
    assert 'version = "0.2.0"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "0.2.0"' in init_file.read_text(encoding="utf-8")


def test_binary_resolve_install_source_prefers_latest_build(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    dist_dir.mkdir(parents=True)
    binary_path = dist_dir / "codepilot.exe"
    binary_path.write_text("exe", encoding="utf-8")

    monkeypatch.setattr(binary_mod, "running_binary_path", lambda: None)
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Windows")

    resolved = binary_mod.resolve_install_source(None, project_root=tmp_path)

    assert resolved == binary_path.resolve()


def test_install_binary_copies_file_and_registers_path(tmp_path, monkeypatch):
    source = tmp_path / "codepilot"
    source.write_text("binary", encoding="utf-8")
    target_dir = tmp_path / "bin"
    captured = {}

    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        binary_mod,
        "register_install_dir",
        lambda directory: captured.update({"directory": Path(directory)}) or (True, "ok"),
    )

    result = binary_mod.install_binary(binary_path=source, target_dir=target_dir, register_path=True)

    assert result.installed_path == (target_dir / "codepilot").resolve()
    assert result.installed_path.exists()
    assert captured["directory"] == target_dir.resolve()


def test_binary_build_command_invokes_pyinstaller(tmp_path, monkeypatch):
    (tmp_path / "codepilot").mkdir()
    (tmp_path / "codepilot" / "__main__.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "codepilot" / "templates").mkdir()
    (tmp_path / "codepilot" / "templates" / "demo.md").write_text("x", encoding="utf-8")
    dist_dir = tmp_path / "dist-out"
    build_dir = tmp_path / "build-out"
    captured = {}

    monkeypatch.setattr(binary_mod, "default_build_dir", lambda root: build_dir)
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(binary_mod.platform, "machine", lambda: "x86_64")

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        binary_path = dist_dir / "codepilot"
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        binary_path.write_text("exe", encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(binary_mod.subprocess, "run", fake_run)

    result = binary_mod.build_binary(project_root=tmp_path, output_dir=dist_dir, clean=True)

    assert result.binary_path == (dist_dir / "codepilot").resolve()
    assert "--onefile" in captured["cmd"]
    assert "--collect-all" in captured["cmd"]


def test_binary_where_command_prints_default_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_INSTALL_DIR", str(tmp_path / "custom-bin"))
    runner = CliRunner()

    result = runner.invoke(main, ["binary", "where"])

    assert result.exit_code == 0
    assert str((tmp_path / "custom-bin").resolve()) in result.output


def test_resolve_release_inputs_uses_dist_binaries_by_default(tmp_path, monkeypatch):
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    linux_dir = tmp_path / "dist" / "binary" / "linux-x86_64"
    win_dir.mkdir(parents=True)
    linux_dir.mkdir(parents=True)
    (win_dir / "codepilot.exe").write_text("exe", encoding="utf-8")
    (linux_dir / "codepilot").write_text("bin", encoding="utf-8")

    resolved = binary_mod.resolve_release_inputs(project_root=tmp_path)

    assert resolved == [
        ("linux-x86_64", (linux_dir / "codepilot").resolve()),
        ("windows-x86_64", (win_dir / "codepilot.exe").resolve()),
    ]


def test_create_release_bundle_generates_manifest_checksums_and_archives(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    output_dir = tmp_path / "release"

    result = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=output_dir,
        version="1.2.3",
    )

    assert result.release_dir == output_dir.resolve()
    assert result.manifest_path.exists()
    assert result.checksum_path.exists()
    assert result.guide_path.exists()
    assert result.summary_path.exists()
    assert result.ai_guide_path.exists()
    assert result.ai_manifest_path.exists()
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.staged_path.exists()
    assert artifact.archive_path.exists()
    assert artifact.archive_format == "zip"
    install_script = output_dir / "windows-x86_64" / "install-codepilot.cmd"
    assert install_script.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.3"
    assert manifest["artifacts"][0]["platform"] == "windows-x86_64"
    assert manifest["artifacts"][0]["archive_format"] == "zip"
    assert "install_script" in manifest["artifacts"][0]
    checksums = result.checksum_path.read_text(encoding="utf-8")
    assert "windows-x86_64/codepilot.exe" in checksums.replace("\\", "/")
    assert artifact.archive_path.name in checksums
    assert "发布说明" in result.guide_path.read_text(encoding="utf-8")
    assert "AI 调用手册" in result.ai_guide_path.read_text(encoding="utf-8")
    assert json.loads(result.ai_manifest_path.read_text(encoding="utf-8"))["name"] == "CodePilot"
    assert "发布摘要" in result.summary_path.read_text(encoding="utf-8")


def test_create_release_bundle_ai_manifest_matches_release_version_and_name(tmp_path):
    binary_path = tmp_path / "pilot.exe"
    binary_path.write_text("binary", encoding="utf-8")

    result = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "release-custom",
        version="1.2.3",
        name="mypilot",
    )

    payload = json.loads(result.ai_manifest_path.read_text(encoding="utf-8"))
    release_prepare = next(item for item in payload["commands"] if item["name"] == "release_prepare")
    release_bundle = next(item for item in payload["commands"] if item["name"] == "release_bundle")

    assert payload["version"] == "1.2.3"
    assert payload["command_name"] == "mypilot"
    assert payload["structured_outputs"][0]["command"] == "mypilot ai manifest"
    assert release_prepare["syntax"] == "mypilot release prepare --version <版本号>"
    assert "dist/binary/linux-x86_64/mypilot" in release_bundle["examples"][1]


def test_binary_release_command_packages_existing_builds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    win_dir.mkdir(parents=True)
    (win_dir / "codepilot.exe").write_text("exe", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--version", "9.9.9"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-9.9.9"
    assert release_dir.exists()
    assert (release_dir / "release.json").exists()
    assert (release_dir / "SHA256SUMS.txt").exists()
    assert (release_dir / "README.zh-CN.md").exists()
    assert (release_dir / "AI_USAGE.zh-CN.md").exists()
    assert (release_dir / "AI_MANIFEST.json").exists()
    assert (release_dir / "SUMMARY.zh-CN.md").exists()
    assert (release_dir / "windows-x86_64" / "install-codepilot.cmd").exists()
    assert "guide:" in result.output
    assert "ai guide:" in result.output
    assert "ai manifest:" in result.output
    assert "summary:" in result.output


def test_verify_release_bundle_passes_for_valid_release(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "release",
        version="1.0.0",
    )

    verification = binary_mod.verify_release_bundle(release.release_dir)

    assert verification.issues == []
    assert verification.checked_files == 2


def test_binary_verify_command_fails_on_broken_checksum(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "dist" / "release" / "codepilot-1.0.0",
        version="1.0.0",
    )
    release.checksum_path.write_text("broken line\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "verify", "--release-dir", str(release.release_dir)])

    assert result.exit_code != 0
    assert "校验失败" in result.output


def test_verify_release_bundle_fails_when_archive_missing_expected_files(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "release",
        version="1.0.0",
    )
    archive_path = release.artifacts[0].archive_path
    with ZipFile(archive_path, "w") as bundle:
        bundle.writestr("broken/file.txt", "x")

    verification = binary_mod.verify_release_bundle(release.release_dir)

    assert any("压缩包缺少预期文件" in issue or "压缩包校验不匹配" in issue for issue in verification.issues)


def test_binary_release_build_current_merges_new_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    win_dir.mkdir(parents=True)
    existing = win_dir / "codepilot.exe"
    existing.write_text("old", encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "linux-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot"
    built_binary.write_text("new", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="linux-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--build-current", "--version", "2.0.0"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-2.0.0"
    assert (release_dir / "windows-x86_64" / "codepilot.exe").exists()
    assert (release_dir / "linux-x86_64" / "codepilot").exists()


def test_binary_release_build_current_works_without_existing_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--build-current", "--version", "3.0.0"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-3.0.0"
    assert (release_dir / "windows-x86_64" / "codepilot.exe").exists()


def test_create_release_bundle_uses_tar_gz_for_linux(tmp_path):
    binary_path = tmp_path / "codepilot"
    binary_path.write_text("binary", encoding="utf-8")

    result = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("linux-x86_64", binary_path)],
        output_dir=tmp_path / "release-linux",
        version="1.0.0",
    )

    artifact = result.artifacts[0]
    assert artifact.archive_format == "tar.gz"
    assert artifact.archive_path.name.endswith(".tar.gz")
    assert (result.release_dir / "linux-x86_64" / "install-codepilot.sh").exists()


def test_binary_prepare_command_updates_version_and_verifies_release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    (package_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "prepare", "--version", "1.2.0"])

    assert result.exit_code == 0
    assert "版本已更新" in result.output
    assert 'version = "1.2.0"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "1.2.0"' in (package_dir / "__init__.py").read_text(encoding="utf-8")
    assert (tmp_path / "dist" / "release" / "codepilot-1.2.0" / "release.json").exists()
    assert "发布目录校验通过" in result.output


def test_binary_prepare_rolls_back_version_when_build_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    pyproject = tmp_path / "pyproject.toml"
    init_file = package_dir / "__init__.py"
    pyproject.write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    init_file.write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    monkeypatch.setattr("codepilot.commands.binary.build_binary", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("build failed")))

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "prepare", "--version", "2.0.0"])

    assert result.exit_code != 0
    assert 'version = "0.1.0"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "0.1.0"' in init_file.read_text(encoding="utf-8")


def test_release_prepare_alias_invokes_prepare(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    (package_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["release", "prepare", "--version", "1.3.0"])

    assert result.exit_code == 0
    assert (tmp_path / "dist" / "release" / "codepilot-1.3.0" / "release.json").exists()


def test_release_verify_alias_invokes_verify(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "dist" / "release" / "codepilot-1.0.0",
        version="1.0.0",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["release", "verify", "--release-dir", str(release.release_dir)])

    assert result.exit_code == 0
    assert "发布目录校验通过" in result.output


# ── Task dedup tests ──────────────────────────────────────────────────────────


def test_create_task_dedup_returns_existing_backlog_task(tmp_path, monkeypatch):
    """Submitting the same project+title+content while a backlog task exists returns
    the original task id instead of creating a duplicate."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X", content="details")
    second = db.create_task("demo", "implement feature X", content="details")

    assert first["id"] == second["id"]
    assert first["dedup_key"] == second["dedup_key"]
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 1


def test_create_task_dedup_allows_resubmit_after_done(tmp_path, monkeypatch):
    """A done task with the same dedup_key should NOT block creating a new task."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X", content="details")
    db.update_task(first["id"], status="done")

    second = db.create_task("demo", "implement feature X", content="details")

    assert second["id"] != first["id"]
    assert second["dedup_key"] == first["dedup_key"]


def test_create_task_dedup_allows_resubmit_after_failed(tmp_path, monkeypatch):
    """A failed task with the same dedup_key should NOT block creating a new task."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X", content="details")
    db.update_task(first["id"], status="failed")

    second = db.create_task("demo", "implement feature X", content="details")

    assert second["id"] != first["id"]


def test_create_task_dedup_blocks_in_progress_duplicate(tmp_path, monkeypatch):
    """An in_progress task with the same dedup_key should block creating a duplicate."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X", content="details")
    db.update_task(first["id"], status="in_progress")

    second = db.create_task("demo", "implement feature X", content="details")

    assert second["id"] == first["id"]


def test_create_task_dedup_prints_notice(tmp_path, monkeypatch, capsys):
    """When returning an existing task, create_task prints [i] 已存在任务 #N."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "implement feature X")
    db.create_task("demo", "implement feature X")

    captured = capsys.readouterr()
    assert f"[i] 已存在任务 #{first['id']}" in captured.out


def test_create_task_respects_caller_supplied_dedup_key(tmp_path, monkeypatch):
    """When a caller provides an explicit dedup_key it is used as-is."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_task("demo", "task A", dedup_key="custom-key-1234")
    second = db.create_task("demo", "task B", dedup_key="custom-key-1234")

    assert first["id"] == second["id"]
    assert first["dedup_key"] == "custom-key-1234"
