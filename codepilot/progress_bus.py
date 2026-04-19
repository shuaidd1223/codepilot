"""A tiny pub/sub bus for execution progress events.

Before this module, CLI status was scattered across direct ``echo()`` /
``STATUS_CONSOLE.print()`` calls and Web UI relied on a single global hook
(``ai._planner_progress_callback``) that could only ever have one subscriber.
The result: live execution output wasn't surfaced to the Web UI job log, and
Web UI's job log didn't reflect recon / review-loop transitions.

This module replaces both with a single publish/subscribe channel. Any code
that does work the user should see calls :func:`emit`; CLI tasks, Web UI
workers, and the SSE endpoint all :func:`subscribe` to receive those events.

Event shape (``dict``):

* ``timestamp`` (ISO string)
* ``task_id`` (int or None — None for pre-task signals like planner progress)
* ``stage`` (``planner`` | ``recon`` | ``clarify`` | ``builder`` | ``reviewer``
  | ``commit`` | ``merge`` | ``system``)
* ``level`` (``info`` | ``warning`` | ``error`` | ``heartbeat``)
* ``message`` (str — short human-readable line)
* ``extra`` (dict — optional structured payload: ``round``, ``round_total``,
  ``pid``, ``exit_code``, etc.)

The bus is intentionally in-process and stateless (no buffering of past
events) — subscribers that miss an event don't get it retroactively. Long-
running UIs (Web UI job log, SSE endpoint) keep their own buffer.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, Optional


# ``Event`` is documented as a dict rather than a dataclass so JSON
# serialization / WebSocket transport stays a single ``json.dumps`` call.
Event = dict
Subscriber = Callable[[Event], None]


_LOCK = threading.Lock()
_SUBSCRIBERS: dict[int, Subscriber] = {}
_NEXT_TOKEN = 1


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit(
    *,
    stage: str,
    message: str,
    task_id: Optional[int] = None,
    level: str = "info",
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Publish one event to every current subscriber.

    Subscriber exceptions are swallowed per-subscriber so a broken Web UI
    consumer never stalls the terminal.
    """
    event: Event = {
        "timestamp": _now_iso(),
        "task_id": task_id,
        "stage": stage,
        "level": level,
        "message": message,
        "extra": dict(extra or {}),
    }
    with _LOCK:
        targets = list(_SUBSCRIBERS.values())
    for cb in targets:
        try:
            cb(event)
        except Exception:
            # One bad subscriber must not block the rest.
            continue


def subscribe(callback: Subscriber) -> int:
    """Register *callback* and return a token usable with :func:`unsubscribe`."""
    global _NEXT_TOKEN
    with _LOCK:
        token = _NEXT_TOKEN
        _NEXT_TOKEN += 1
        _SUBSCRIBERS[token] = callback
    return token


def unsubscribe(token: int) -> None:
    """Remove a subscriber. Safe to call twice / with an unknown token."""
    with _LOCK:
        _SUBSCRIBERS.pop(token, None)


class subscription:
    """Context-manager wrapper so workers can ``with subscribe(cb):`` safely."""

    def __init__(self, callback: Subscriber):
        self._callback = callback
        self._token: Optional[int] = None

    def __enter__(self) -> "subscription":
        self._token = subscribe(self._callback)
        return self

    def __exit__(self, *_exc_info) -> None:
        if self._token is not None:
            unsubscribe(self._token)
            self._token = None


def clear_subscribers_for_tests() -> None:
    """Internal: drop all subscribers. Only tests use this to isolate cases."""
    with _LOCK:
        _SUBSCRIBERS.clear()
