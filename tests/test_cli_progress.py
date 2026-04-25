"""CLI-side rendering of progress_bus events.

Without ``cli_progress.cli_renderer`` the heartbeat events emitted from
API-mode LLM calls reach the Web UI but never the terminal, which
recreates the original "system is running but I see nothing" pain.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from codepilot import cli_progress, output as output_mod, progress_bus


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    progress_bus.clear_subscribers_for_tests()
    buf = io.StringIO()
    monkeypatch.setattr(output_mod, "_CONSOLE", None)
    monkeypatch.setattr(output_mod, "_CONSOLE_STREAM", None)
    monkeypatch.setattr(
        output_mod,
        "_build_console",
        lambda stream: Console(file=buf, highlight=False, force_terminal=False, width=200),
    )
    monkeypatch.setattr(output_mod, "_LINE_OPEN", False)
    monkeypatch.setattr(output_mod, "_now_text", lambda: "12:00:00")
    yield buf
    progress_bus.clear_subscribers_for_tests()


def test_renderer_prints_info_events_with_stage_prefix(_reset_state):
    buf = _reset_state
    with cli_progress.cli_renderer():
        progress_bus.emit(stage="reviewer", message="启动 reviewer round 1/3")
    rendered = buf.getvalue()
    assert "12:00:00" in rendered
    assert "<reviewer>" in rendered
    assert "启动 reviewer round 1/3" in rendered


def test_renderer_skips_subprocess_source_events(_reset_state):
    buf = _reset_state
    with cli_progress.cli_renderer():
        progress_bus.emit(
            stage="builder",
            message="raw subprocess line",
            extra={"source": "subprocess"},
        )
        progress_bus.emit(
            stage="builder",
            message="",
            extra={"task_log_stream": True, "task_log_chunk": "..."},
        )
    rendered = buf.getvalue()
    assert "raw subprocess line" not in rendered
    assert rendered == "" or rendered.strip() == ""


def test_renderer_renders_llm_heartbeat_summary(_reset_state):
    buf = _reset_state
    with cli_progress.cli_renderer():
        progress_bus.emit(
            stage="clarify",
            level="heartbeat",
            message="provider 生成中",
            extra={
                "llm_heartbeat": True,
                "provider": "claude-sonnet",
                "elapsed_seconds": 2.4,
                "estimated_tokens": 180,
            },
        )
    rendered = buf.getvalue()
    assert "<clarify>" in rendered
    assert "claude-sonnet" in rendered
    assert "2.4s" in rendered
    assert "~180 tokens" in rendered


def test_renderer_throttles_repeat_heartbeats(_reset_state):
    buf = _reset_state
    extra = {"llm_heartbeat": True, "provider": "p", "elapsed_seconds": 0.1, "estimated_tokens": 5}
    with cli_progress.cli_renderer():
        progress_bus.emit(stage="planner", level="heartbeat", message="p1", extra=dict(extra))
        progress_bus.emit(stage="planner", level="heartbeat", message="p2", extra=dict(extra))
        progress_bus.emit(stage="planner", level="heartbeat", message="p3", extra=dict(extra))
        # final=True should always render even within the throttle window.
        progress_bus.emit(
            stage="planner",
            level="heartbeat",
            message="done",
            extra={**extra, "final": True, "estimated_tokens": 12, "elapsed_seconds": 0.4},
        )
    rendered = buf.getvalue()
    # Two visible lines: the first heartbeat and the final one. Throttled
    # mid-stream heartbeats must be coalesced.
    assert rendered.count("<planner>") == 2
    assert "0.4s" in rendered


def test_renderer_unsubscribes_on_exit(_reset_state):
    buf = _reset_state
    with cli_progress.cli_renderer():
        progress_bus.emit(stage="system", message="inside")
    assert "inside" in buf.getvalue()

    # After the context exits the renderer must be detached so background
    # emits never reach the terminal again.
    progress_bus.emit(stage="system", message="leaked")
    assert "leaked" not in buf.getvalue()
