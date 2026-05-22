from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.ai_support.agent_support import ai_guide_markdown, command_manifest
from codepilot.binary_support import manager as binary_mod
from codepilot.binary_support import paths as binary_paths_mod
from codepilot.storage import database as db
from codepilot.ai_support import providers as providers_mod
from codepilot.ai_support import service as ai_mod
from codepilot.core import progress_bus
from codepilot.gateway.service import GatewayResponse
from codepilot.core import runtime as runtime_mod
from codepilot.webapp import server as webui_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.core.config import load_project_config
from tests.workflow_testkit import init_test_db as _init_test_db


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
    """Verdict extraction recognises the three strong signals documented in
    reviewer_rules.md:

    1. Explicit ``VERDICT: PASS|FAIL`` anchor (required by the reviewer
       prompt, takes precedence over everything else).
    2. A ``需要修复的点 / 需要修复 / 需要处理 / 修复建议`` section header,
       which the reviewer opens only when it intends to block.
    3. Non-blocking observations (e.g. bare ``- [PX]`` bullets) are NOT
       treated as FAIL anymore — that older heuristic misfired on
       informational notes and caused spurious retries.
    """
    # Strong FAIL: explicit section header.
    fail_header = "需要修复的点:\n- [P1] Keep add returning a sum"
    # Strong FAIL: explicit verdict line.
    verdict_fail = "**VERDICT: FAIL**"
    # PASS: short affirmative prose, no blocker anchor.
    pass_output = "The only change adds a comment and does not affect behavior."
    # Observation-only: bullets without a section header should NOT fail.
    soft_note = "Review comment:\n- [P1] Keep add returning a sum"

    assert run_cmd._extract_review_verdict(fail_header, "codex-review") == "fail"
    assert run_cmd._extract_review_verdict(verdict_fail, "claude-review") == "fail"
    assert run_cmd._extract_review_verdict(pass_output, "codex-review") == "pass"
    assert run_cmd._extract_review_verdict(soft_note, "codex-review") == "pass"


def test_run_builtin_executor_fails_when_review_verdict_is_unknown(monkeypatch, tmp_path):
    """When the reviewer returns empty output, the verdict extractor emits
    ``unknown`` and the executor stops with exit_code=2 + a clear summary.

    Previously this test used a short non-empty string ("没有输出 verdict"),
    which — under the current verdict policy — is treated as PASS because
    it carries no explicit FAIL anchor. Empty output is the canonical
    "reviewer broke / produced nothing" signal we want to flag as unknown.
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    task_file = project_path / "task.md"
    task_file.write_text("demo", encoding="utf-8")

    phases = iter(
        [
            ("codex", 0, "builder ok"),
            ("claude-review", 0, ""),
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


def test_builtin_runtime_dir_is_project_first_and_outside_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    project_path = tmp_path / "project"
    project_path.mkdir()

    runtime_dir = run_cmd._builtin_runtime_dir({"name": "demo", "path": str(project_path)})

    assert runtime_dir.exists()
    assert not runtime_dir.is_relative_to(project_path)
    assert runtime_dir == tmp_path / "home" / ".codepilot" / "data" / "demo" / "runs"


def test_find_dispatch_script_checks_project_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("CODEPILOT_DISPATCH_PATH", raising=False)
    project_path = tmp_path / "repo-dir"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"
""".strip(),
        encoding="utf-8",
    )
    project_script = tmp_path / "home" / ".codepilot" / "data" / "demo" / "scripts" / "task-dispatch.sh"
    project_script.parent.mkdir(parents=True)
    project_script.write_text("#!/usr/bin/env sh\n", encoding="utf-8")

    assert run_cmd._find_dispatch_script(str(project_path)) == project_script


def test_pick_untracked_task_file_uses_project_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    project_path = tmp_path / "repo-dir"
    project_path.mkdir()

    task_file = run_cmd._pick_task_file(
        project_path,
        7,
        tracked=False,
        project={"name": "demo", "path": str(project_path)},
    )

    assert task_file == tmp_path / "home" / ".codepilot" / "data" / "demo" / "task-files" / "007-task.md"


def test_task_payload_includes_blocked_reason_for_dirty_worktree():
    from codepilot.webapp.task_payloads import _task_payload

    task = {
        "id": 1,
        "project": "demo",
        "title": "test",
        "status": "backlog",
        "priority": "P2",
        "agent": "dual",
        "source": "user",
        "error_message": (
            "内置执行器检测到主工作区已有未提交改动。"
            "当前预检策略为 stop，本次跳过执行且不消耗重试次数。"
        ),
    }
    payload = _task_payload(task)
    assert payload.get("blocked_reason") is not None
    assert "stop" in payload["blocked_reason"]
    assert len(payload.get("suggested_actions") or []) >= 2


def test_preflight_blocked_detail_parses_dirty_worktree_error():
    from codepilot.commands.run_builtin_core import _preflight_blocked_detail

    dirty = (
        "内置执行器检测到主工作区已有未提交改动。"
        "当前预检策略为 stop，本次跳过执行且不消耗重试次数。"
    )
    result = _preflight_blocked_detail(dirty)
    assert result is not None
    assert result["blocked"] is True
    assert "stop" in result["reason"]
    assert len(result["suggested_actions"]) >= 2
    assert any("commit" in a for a in result["suggested_actions"])
    assert any("stash" in a for a in result["suggested_actions"])
    assert any("preflight_dirty_worktree" in a for a in result["suggested_actions"])

    assert _preflight_blocked_detail("正常执行失败") is None
    assert _preflight_blocked_detail("") is None
    assert _preflight_blocked_detail(None) is None


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


def test_generate_task_content_uses_project_configured_codex_command_when_path_missing(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake_codex = tmp_path / "tools" / "codex.cmd"
    fake_codex.parent.mkdir()
    fake_codex.write_text("@echo off\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        f"""
[project]
name = "demo"

[agents.commands]
codex = "{fake_codex.as_posix()}"
""".strip(),
        encoding="utf-8",
    )

    captured = {}
    monkeypatch.setattr(ai_mod, "_collect_project_context", lambda project_path: "")
    monkeypatch.setattr(providers_mod.shutil, "which", lambda cmd: None)

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


def test_run_builtin_phase_uses_project_configured_codex_command_when_path_missing(monkeypatch, tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake_codex = tmp_path / "tools" / "codex.cmd"
    fake_codex.parent.mkdir()
    fake_codex.write_text("@echo off\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        f"""
[project]
name = "demo"

[agents.commands]
codex = "{fake_codex.as_posix()}"
""".strip(),
        encoding="utf-8",
    )

    captured = {}
    monkeypatch.setattr(run_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(providers_mod.shutil, "which", lambda cmd: None)

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


def test_run_builtin_phase_injects_current_task_guard_env(monkeypatch, tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.delenv("CODEPILOT_RUNNER_TASK_ID", raising=False)
    monkeypatch.delenv("CODEPILOT_RUNNER_PHASE", raising=False)

    captured: dict[str, str | None] = {}
    monkeypatch.setattr(run_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))

    class DummyProvider:
        @staticmethod
        def find_executable():
            return Path("C:/fake/codex.CMD")

    monkeypatch.setattr(run_cmd, "resolve_cli_provider", lambda provider_key, project_path=None: DummyProvider())
    monkeypatch.setattr(run_cmd, "_read_output_file", lambda path: "")

    def fake_run_command_live(cmd, **kwargs):
        captured["task_id_env"] = os.environ.get("CODEPILOT_RUNNER_TASK_ID")
        captured["phase_env"] = os.environ.get("CODEPILOT_RUNNER_PHASE")
        captured["phase_arg"] = kwargs.get("phase")
        return 0, "console ok"

    monkeypatch.setattr(run_cmd, "_run_command_live", fake_run_command_live)

    label, exit_code, output = run_cmd._run_builtin_phase(
        task={"id": 42, "agent": "codex"},
        project_path=project_path,
        phase="builder",
        prompt="implement",
        output_path=project_path / "builder.txt",
        timeout=30,
    )

    assert label == "codex"
    assert exit_code == 0
    assert output == "console ok"
    assert captured == {
        "task_id_env": "42",
        "phase_env": "builder",
        "phase_arg": "builder",
    }
    assert os.environ.get("CODEPILOT_RUNNER_TASK_ID") is None
    assert os.environ.get("CODEPILOT_RUNNER_PHASE") is None


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


@pytest.mark.parametrize("terminal_status", ["done", "failed", "cancelled"])
def test_update_task_runtime_ignores_late_heartbeat_after_terminal_status(tmp_path, monkeypatch, terminal_status):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    final_log = tmp_path / "final.log"
    final_log.write_text("final output", encoding="utf-8")
    late_log = tmp_path / "late.log"
    late_log.write_text("late output", encoding="utf-8")

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "late heartbeat", agent="dual")
    db.update_task(
        task["id"],
        status=terminal_status,
        completed_at="2026-05-22T10:00:00",
        run_phase=None,
        heartbeat_at=None,
        active_pid=None,
        current_log_path=str(final_log),
        last_output="final output",
    )

    runtime_mod.update_task_runtime(
        task["id"],
        phase="reviewer",
        pid=222222,
        log_path=late_log,
        last_output="late heartbeat",
        heartbeat_at=datetime(2026, 5, 22, 10, 1, 0),
    )

    current = db.get_task(task["id"])
    assert current["status"] == terminal_status
    assert current["run_phase"] is None
    assert current["heartbeat_at"] is None
    assert current["active_pid"] is None
    assert current["current_log_path"] == str(final_log)
    assert current["last_output"] == "final output"


def test_run_backlog_builtin_success_clears_live_runtime_fields(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    live_log = tmp_path / "review.log"
    live_log.write_text("review output", encoding="utf-8")

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "success clears runtime", agent="dual")

    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "")
    monkeypatch.setattr(run_cmd, "_cleanup_worktree_leftovers", lambda *args, **kwargs: None)

    def fake_executor(task_row, *args, **kwargs):
        runtime_mod.update_task_runtime(
            task_row["id"],
            phase="reviewer",
            pid=333333,
            log_path=live_log,
            last_output="review still running",
        )
        return run_cmd.ExecutionResult(exit_code=0, output="ok", summary="done", executor="builtin")

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_executor)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["done"] == 1
    assert current["status"] == "done"
    assert current["run_phase"] is None
    assert current["heartbeat_at"] is None
    assert current["active_pid"] is None
    assert current["current_log_path"] == str(live_log)


def test_run_backlog_cleans_worktree_leftovers_after_builtin_failure(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "fail and clean", agent="dual", max_retries=3)

    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=1,
            output="builder crashed",
            summary="builder crashed",
            executor="builtin",
        ),
    )
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(
        run_cmd,
        "_cleanup_worktree_leftovers",
        lambda worktree_path, project_path, *, task_id: cleanup_calls.append(
            {
                "worktree_path": str(worktree_path),
                "project_path": str(project_path),
                "task_id": task_id,
            }
        ),
    )
    # Skip AI review triage so this regression test exercises the cleanup
    # path under the legacy retry semantics it was written for.
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *a, **kw: None)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert cleanup_calls == [
        {
            "worktree_path": str(project_path.resolve()),
            "project_path": str(project_path.resolve()),
            "task_id": task["id"],
        }
    ]
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1
    assert current["run_phase"] is None
    assert current["active_pid"] is None
    assert current["current_log_path"] is None


def test_builder_done_review_timeout_preserves_builder_evidence(tmp_path, monkeypatch):
    """Orchestrator must not lose builder success evidence on reviewer timeout."""
    import subprocess

    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# repo", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True)

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "review timeout test", agent="dual", max_retries=2)

    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "")

    class _TimeoutExecutor:
        def __call__(self, *args, **kwargs):
            return run_cmd.ExecutionResult(
                exit_code=1,
                output="builder success evidence",
                summary="✅ Builder 已完成但 ❌ Reviewer 超时，可以重试 review 或接受 builder 结果",
                executor="builtin",
            )

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", _TimeoutExecutor())
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *a, **kw: None)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert "Builder 已完成" in (current.get("error_message") or ""), "Error message must mention builder done"
    assert "超时" in (current.get("error_message") or ""), "Error message must mention timeout"
    # Builder evidence is preserved in result.output; the orchestrator emits it
    # via _emit_phase_summaries so it appears in console / task logs. The
    # error_message must clearly distinguish timeout from regular failure.
    assert current["status"] == "failed", "Task must be marked as failed (not backlog) to prevent auto-retry"
    assert stats.get("failed") == 1, "Stats must report 1 failed task"
    assert stats.get("requeued", 0) == 0, "Timeout must not requeue task"


def test_builder_done_review_tool_failure_preserves_builder_evidence(tmp_path, monkeypatch):
    """Reviewer tooling failures after a successful builder must not requeue."""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "review tool failure test", agent="dual", max_retries=2)

    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "")

    phases = iter(
        [
            ("codex", 0, "builder success evidence"),
            OSError("[Errno 22] Invalid argument"),
        ]
    )

    def fake_builtin_phase(**kwargs):
        phase = next(phases)
        if isinstance(phase, BaseException):
            raise phase
        return phase

    monkeypatch.setattr(run_cmd, "_run_builtin_phase", fake_builtin_phase)
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *a, **kw: None)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    logs = db.list_task_logs(task["id"])
    error_message = current.get("error_message") or ""

    assert current["status"] == "failed", "Reviewer tool failure must stop instead of requeueing dirty builder output"
    assert stats.get("failed") == 1
    assert stats.get("requeued", 0) == 0
    assert "Builder 已完成" in error_message
    assert "Reviewer 工具失败" in error_message
    assert "[Errno 22] Invalid argument" in error_message
    assert "重试 review" in error_message
    assert "切换 reviewer" in error_message
    assert "人工接受/提交" in error_message
    assert any(log["phase"] == "builder" and "builder success evidence" in (log["output"] or "") for log in logs)
    assert any(log["phase"] == "reviewer" and "[Errno 22] Invalid argument" in (log["output"] or "") for log in logs)


def test_builder_done_reviewer_tooling_failure_does_not_requeue_dirty_backlog(tmp_path, monkeypatch):
    """Builder success + reviewer tooling failure must not requeue into dirty preflight."""
    import subprocess

    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    (project_path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_path, capture_output=True, check=True)
    base_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=project_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    config_file = project_path / "AGENTS.toml"
    config_file.write_text(
        f"""
[project]
name = "demo"
base_branch = "{base_branch}"

[automation]
task_workspace = "direct"
per_task_branch = false
preflight_dirty_worktree = "stop"
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path), base_branch=base_branch, config_file=str(config_file))
    task = db.create_task("demo", "review tool failure test", agent="dual", max_retries=2)

    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "")
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *a, **kw: None)

    def fake_executor(*args, **kwargs):
        (project_path / "README.md").write_text("# repo\n\nbuilder change\n", encoding="utf-8")
        return run_cmd.ExecutionResult(
            exit_code=1,
            output="builder success evidence",
            review_output="[Errno 22] Invalid argument",
            summary="✅ Builder 已完成但 ❌ Reviewer 工具失败，可以重试 review、切换 reviewer 或人工接受/提交补丁",
            executor="builtin",
        )

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_executor)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert current["status"] == "failed"
    assert stats.get("failed") == 1
    assert stats.get("requeued", 0) == 0
    assert "Builder 已完成" in (current.get("error_message") or "")
    assert "Reviewer 工具失败" in (current.get("error_message") or "")
    assert "重试 review" in (current.get("error_message") or "")
    assert "切换 reviewer" in (current.get("error_message") or "")
    assert "builder change" in (project_path / "README.md").read_text(encoding="utf-8")
