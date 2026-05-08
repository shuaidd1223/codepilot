from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.ai_support.agent_support import ai_guide_markdown, command_manifest
from codepilot.binary_support import manager as binary_mod
from codepilot.binary_support import paths as binary_paths_mod
from codepilot.storage import database as db
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


def test_generate_task_content_uses_project_configured_codex_command(tmp_path, monkeypatch):
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


def test_run_builtin_phase_uses_project_configured_codex_command(monkeypatch, tmp_path):
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

