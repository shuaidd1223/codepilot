"""Tests for the simplified run_live_runner after removing _MarkdownLiveWriter.

Verifies that the direct pass-through pipeline works:
- emit() writes raw text directly (no markdown transformation)
- emit_run_status() sends lightweight SSE events
- finalize() writes footer without _MarkdownLiveWriter
- _LiveOutputProcessor has no md_live_writer attribute
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from codepilot.commands import run as run_cmd
from codepilot.core.workflow_state import read_task_execution_artifacts
from codepilot.commands.run_live_runner import (
    _LiveOutputProcessor,
)
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def test_processor_has_no_markdown_live_writer():
    """_LiveOutputProcessor must not have md_live_writer after redesign."""
    handle = io.StringIO()
    output = _LiveOutputProcessor(
        task_id=1, phase="builder", handle=handle, raw_chunk_size=64,
    )
    assert not hasattr(output, "md_live_writer"), (
        "_LiveOutputProcessor should not have md_live_writer attribute"
    )


def test_emit_writes_raw_text_directly():
    """emit() writes raw text directly — no ~~~text wrapping, no role sections."""

    handle = io.StringIO()
    output = _LiveOutputProcessor(
        task_id=1, phase="builder", handle=handle, raw_chunk_size=64,
    )

    # Patch _emit_log_stream to avoid the real progress_bus call
    output._emit_log_stream = lambda *a, **kw: None
    # Patch _emit_progress_line to avoid the real progress_bus call
    output._emit_progress_line = lambda *a, **kw: None

    raw_text = "## Hello\n```python\nprint('hi')\n```\n"
    output.emit(raw_text)

    written = handle.getvalue()
    assert "## Hello" in written
    assert "```python" in written
    assert "~~~text" not in written, "raw output must not be wrapped in ~~~text"
    assert "### Runtime" not in written, "raw output must not have role sections"


def test_emit_updates_offset():
    """emit() increments emitted_log_bytes by the byte length of raw."""
    handle = io.StringIO()
    output = _LiveOutputProcessor(
        task_id=1, phase="builder", handle=handle, raw_chunk_size=64,
    )
    output._emit_log_stream = lambda *a, **kw: None
    output._emit_progress_line = lambda *a, **kw: None
    assert output.emitted_log_bytes == 0
    output.emit("hello")
    assert output.emitted_log_bytes == len("hello".encode("utf-8"))


def test_emit_run_status_sends_correct_event():
    """emit_run_status() sends a progress_bus event with source=task_run_status."""
    handle = io.StringIO()
    output = _LiveOutputProcessor(
        task_id=42, phase="builder", handle=handle, raw_chunk_size=64,
    )

    mock_bus = MagicMock()
    # Replace _emit_log_stream and patch the import in emit_run_status
    import codepilot.core.progress_bus as pb_mod
    old_emit, pb_mod.emit = pb_mod.emit, mock_bus.emit
    try:
        output.emit_run_status(elapsed_seconds=30, silent_seconds=12)
    finally:
        pb_mod.emit = old_emit

    mock_bus.emit.assert_called_once()
    kwargs = mock_bus.emit.call_args.kwargs
    assert kwargs["task_id"] == 42
    assert kwargs["stage"] == "builder"
    assert kwargs["extra"]["source"] == "task_run_status"
    assert kwargs["extra"]["elapsed_seconds"] == 30
    assert kwargs["extra"]["silent_seconds"] == 12


def test_emit_run_status_does_not_write_to_handle():
    """emit_run_status() must never write to the log file handle."""
    handle = io.StringIO()
    output = _LiveOutputProcessor(
        task_id=1, phase="builder", handle=handle, raw_chunk_size=64,
    )

    import codepilot.core.progress_bus as pb_mod
    old_emit, pb_mod.emit = pb_mod.emit, MagicMock().emit
    try:
        output.emit_run_status(elapsed_seconds=5, silent_seconds=3)
        output.emit_run_status(elapsed_seconds=10, silent_seconds=8)
    finally:
        pb_mod.emit = old_emit

    assert handle.getvalue() == "", (
        "emit_run_status must not write heartbeat lines to the log file"
    )


def test_finalize_writes_footer():
    """finalize() writes ## Result footer."""
    handle = io.StringIO()
    output = _LiveOutputProcessor(
        task_id=1, phase="builder", handle=handle, raw_chunk_size=64,
    )

    output.finalize(status="ok", exit_code=0, detail="all good")
    written = handle.getvalue()
    assert "## Result" in written
    assert "status: `ok`" in written
    assert "exit_code: `0`" in written
    assert output.footer_written is True


def test_finalize_is_idempotent():
    """finalize() must not write the footer twice."""
    handle = io.StringIO()
    output = _LiveOutputProcessor(
        task_id=1, phase="builder", handle=handle, raw_chunk_size=64,
    )

    output.finalize(status="ok", exit_code=0)
    first = handle.getvalue()
    output.finalize(status="ok", exit_code=0)
    second = handle.getvalue()
    assert first == second, "finalize must be idempotent"


def test_seconds_since_last_output_tracks_idle_time():
    """seconds_since_last_output should increase after emit."""
    import time

    handle = io.StringIO()
    output = _LiveOutputProcessor(
        task_id=1, phase="builder", handle=handle, raw_chunk_size=64,
    )
    output._emit_log_stream = lambda *a, **kw: None
    output._emit_progress_line = lambda *a, **kw: None

    output.emit("hello")
    time.sleep(0.1)
    idle = output.seconds_since_last_output()
    assert idle > 0


def _register_direct_git_project(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    run_cmd._run_command(["git", "init"], cwd=project_path, timeout=60)
    run_cmd._run_command(["git", "config", "user.name", "CodePilot Test"], cwd=project_path, timeout=30)
    run_cmd._run_command(["git", "config", "user.email", "test@example.com"], cwd=project_path, timeout=30)
    (project_path / "README.md").write_text("# demo\n", encoding="utf-8")
    config_file = project_path / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"

[automation]
per_task_branch = false
task_workspace = "direct"
preflight_dirty_worktree = "stop"
""".strip(),
        encoding="utf-8",
    )
    run_cmd._run_command(["git", "add", "README.md", "AGENTS.toml"], cwd=project_path, timeout=30)
    code, output = run_cmd._run_command(["git", "commit", "-m", "init"], cwd=project_path, timeout=120)
    assert code == 0, output
    db.register_project("demo", str(project_path), config_file=str(config_file))
    return project_path


def test_run_records_success_artifacts_with_empty_patch(tmp_path, monkeypatch):
    project_path = _register_direct_git_project(tmp_path, monkeypatch)
    task = db.create_task("demo", "success artifact", agent="claude", max_retries=1)

    def fake_executor(*args, **kwargs):
        return run_cmd.ExecutionResult(
            exit_code=0,
            output="builder ok",
            review_output="all good\nVERDICT: PASS",
            summary="done",
            executor="builtin",
        )

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_executor)
    monkeypatch.setattr(run_cmd, "_cleanup_worktree_leftovers", lambda *args, **kwargs: None)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, quiet=True)

    assert stats["done"] == 1
    artifacts = read_task_execution_artifacts(project_path, task["id"])
    assert artifacts is not None
    assert artifacts["status"] == "done"
    assert artifacts["artifacts"]["patch"]["status"] == "empty"
    assert artifacts["artifacts"]["patch"]["empty"] is True
    assert artifacts["artifacts"]["validation"]["status"] == "passed"
    assert artifacts["artifacts"]["validation"]["checks"][0]["exit_code"] == 0
    assert artifacts["artifacts"]["review"]["verdict"] == "pass"


def test_run_records_failed_artifacts_with_exit_code_and_reviewer_verdict(tmp_path, monkeypatch):
    project_path = _register_direct_git_project(tmp_path, monkeypatch)
    task = db.create_task("demo", "failure artifact", agent="claude", max_retries=1)

    def fake_executor(*args, **kwargs):
        return run_cmd.ExecutionResult(
            exit_code=2,
            output="pytest failed\nE assertion",
            review_output="AC #1: FAIL\nVERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
        )

    monkeypatch.setattr(run_cmd, "_run_builtin_executor", fake_executor)
    monkeypatch.setattr(run_cmd, "_cleanup_worktree_leftovers", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *args, **kwargs: None)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, retry_on_failure=False, quiet=True)

    assert stats["failed"] == 1
    artifacts = read_task_execution_artifacts(project_path, task["id"])
    assert artifacts is not None
    validation = artifacts["artifacts"]["validation"]
    assert validation["status"] == "failed"
    assert validation["checks"][0]["command"] == "builtin"
    assert validation["checks"][0]["exit_code"] == 2
    assert "pytest failed" in validation["checks"][0]["output_excerpt"]
    assert artifacts["artifacts"]["review"]["verdict"] == "fail"
