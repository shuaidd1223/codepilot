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

from codepilot.commands.run_live_runner import (
    _LiveOutputProcessor,
)


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
