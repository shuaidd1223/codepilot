"""Render :mod:`codepilot.progress_bus` events on the CLI.

Without this module, heartbeats emitted from API-mode LLM calls
(``ai_providers._run_api_provider``) reach the Web UI through SSE but
never the terminal — long waits looked like a black box.

The renderer is a context manager so it can wrap a single command run
without leaking subscriptions across the test suite. It deliberately:

* Skips ``source=subprocess`` events — those are already rendered by
  ``run_live_runner._LiveOutputProcessor`` against ``STATUS_CONSOLE``.
* Throttles heartbeat rendering per ``(stage, task_id)`` so a chatty
  streaming response does not flood the terminal.
* Folds repeated heartbeats by overwriting the last line for the
  same key — once another stage emits, a fresh line is started so
  the timeline stays readable.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from typing import Any, Iterator, Optional

from codepilot import progress_bus
from codepilot.output import echo


_HEARTBEAT_THROTTLE_SECONDS = 1.0
_CLI_RENDERER_ACTIVE: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "codepilot_cli_progress_active",
    default=False,
)
_LEVEL_TO_ECHO = {
    "info": "info",
    "warning": "warning",
    "error": "error",
    "heartbeat": "info",
}


def _stage_label(stage: str) -> str:
    return (str(stage or "system").strip() or "system")[:12]


def _heartbeat_summary(event: dict) -> Optional[str]:
    extra = event.get("extra") or {}
    if not extra.get("llm_heartbeat"):
        return None
    elapsed = extra.get("elapsed_seconds")
    tokens = extra.get("estimated_tokens")
    final = extra.get("final")
    provider = extra.get("provider") or ""
    parts: list[str] = []
    if provider:
        parts.append(str(provider))
    if isinstance(elapsed, (int, float)):
        parts.append(f"{float(elapsed):.1f}s")
    if isinstance(tokens, int) and tokens > 0:
        parts.append(f"~{tokens} tokens")
    if final:
        parts.append("done")
    return " · ".join(parts) if parts else None


class _Renderer:
    """Thread-safe renderer; one instance per ``cli_renderer`` context."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_heartbeat_at: dict[tuple[str, Any], float] = {}

    def __call__(self, event: dict) -> None:
        try:
            extra = event.get("extra") or {}
            # ``_LiveOutputProcessor`` already mirrors subprocess output to
            # STATUS_CONSOLE; rendering it here would double-print.
            if extra.get("source") == "subprocess" or extra.get("task_log_stream"):
                return

            stage = _stage_label(event.get("stage"))
            level_raw = str(event.get("level") or "info").lower()
            message = str(event.get("message") or "").strip()

            if level_raw == "heartbeat":
                key = (stage, event.get("task_id"))
                now = time.monotonic()
                with self._lock:
                    last = self._last_heartbeat_at.get(key, 0.0)
                    is_final = bool(extra.get("final"))
                    if not is_final and (now - last) < _HEARTBEAT_THROTTLE_SECONDS:
                        return
                    self._last_heartbeat_at[key] = now
                summary = _heartbeat_summary(event) or message
                if not summary:
                    return
                echo(f"[dim]<{stage}>[/dim] {summary}", level="info")
                return

            if not message:
                return

            echo_level = _LEVEL_TO_ECHO.get(level_raw, "info")
            echo(f"[dim]<{stage}>[/dim] {message}", level=echo_level)
        except Exception:
            # A misformatted event must never break a real run.
            return


@contextlib.contextmanager
def cli_renderer() -> Iterator[None]:
    """Subscribe a CLI-side progress renderer for the duration of the block."""
    renderer = _Renderer()
    with progress_bus.subscription(renderer):
        yield


@contextlib.contextmanager
def maybe_cli_renderer(*, enabled: bool = True) -> Iterator[None]:
    """Attach ``cli_renderer`` once, or no-op when CLI progress is disabled."""
    if not enabled or _CLI_RENDERER_ACTIVE.get():
        yield
        return

    token = _CLI_RENDERER_ACTIVE.set(True)
    try:
        with cli_renderer():
            yield
    finally:
        # ``reset`` is critical: if an exception elsewhere skipped this we'd
        # leak the True flag into the next ``maybe_cli_renderer`` call (in
        # the same thread / async context), making it silently no-op. The
        # ``finally`` already guards the happy path; ``clear_for_tests()``
        # below covers the rare path where pytest reuses the worker thread
        # across tests and the previous test crashed before reaching here.
        _CLI_RENDERER_ACTIVE.reset(token)


def clear_for_tests() -> None:
    """Reset the active-renderer flag. Safe to call from test fixtures.

    pytest reuses the worker thread across tests, so a ContextVar set by an
    earlier test that crashed before its ``finally`` could swallow the next
    test's ``maybe_cli_renderer`` attach (it would think a renderer is
    already active and yield without subscribing). Tests should call this
    in setup/teardown together with ``progress_bus.clear_subscribers_for_tests()``.
    """
    _CLI_RENDERER_ACTIVE.set(False)
