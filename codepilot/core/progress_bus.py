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
* ``stage`` (``planner`` | ``recon`` | ``analysis`` | ``builder`` | ``reviewer``
  | ``commit`` | ``merge`` | ``system``)
* ``type`` (optional ``phase_start`` | ``heartbeat`` | ``phase_end`` | ``error``)
* ``level`` (``info`` | ``warning`` | ``error`` | ``heartbeat``)
* ``message`` (str — short human-readable line)
* ``extra`` (dict — optional structured payload: ``round``, ``round_total``,
  ``pid``, ``exit_code``, etc.)

The bus is intentionally in-process and keeps a short in-memory ring buffer
of recent events. Subscribers can request a replay tail (by event id) to
bridge transient disconnects; long-running UIs may still keep their own
view-specific buffers.
"""

from __future__ import annotations

from collections import deque
import contextlib
import contextvars
import threading
from datetime import datetime
from typing import Any, Callable, Iterator, Optional


# ``Event`` is documented as a dict rather than a dataclass so JSON
# serialization / WebSocket transport stays a single ``json.dumps`` call.
Event = dict
Subscriber = Callable[[Event], None]


_LOCK = threading.Lock()
_SUBSCRIBERS: dict[int, Subscriber] = {}
_NEXT_TOKEN = 1
_NEXT_EVENT_ID = 1
_EVENT_HISTORY_LIMIT = 4096
_EVENT_HISTORY: "deque[Event]" = deque(maxlen=_EVENT_HISTORY_LIMIT)
_UNSET = object()
_LLM_CONTEXT: "contextvars.ContextVar[dict[str, Any] | None]" = contextvars.ContextVar(
    "codepilot_progress_bus_llm_context",
    default=None,
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit(
    *,
    stage: str,
    message: str,
    task_id: Optional[int] = None,
    level: str = "info",
    event_type: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Publish one event to every current subscriber.

    Subscriber exceptions are swallowed per-subscriber so a broken Web UI
    consumer never stalls the terminal.
    """
    global _NEXT_EVENT_ID
    resolved_type = str(event_type or "").strip()
    if not resolved_type:
        if level == "heartbeat":
            resolved_type = "heartbeat"
        elif level == "error":
            resolved_type = "error"
    base: Event = {
        "timestamp": _now_iso(),
        "task_id": task_id,
        "stage": stage,
        "type": resolved_type,
        "level": level,
        "message": message,
        "extra": dict(extra or {}),
    }
    with _LOCK:
        event: Event = dict(base)
        event["id"] = int(_NEXT_EVENT_ID)
        _NEXT_EVENT_ID += 1
        _EVENT_HISTORY.append(dict(event))
        targets = list(_SUBSCRIBERS.values())
    for cb in targets:
        try:
            cb(dict(event))
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


def has_subscribers() -> bool:
    """Return whether there are any active subscribers."""
    with _LOCK:
        return bool(_SUBSCRIBERS)


def current_llm_context() -> dict[str, Any]:
    """Return the current LLM heartbeat context snapshot."""
    return dict(_LLM_CONTEXT.get() or {})


@contextlib.contextmanager
def llm_context(
    *,
    task_id: object = _UNSET,
    stage: object = _UNSET,
    label: object = _UNSET,
) -> Iterator[dict[str, Any]]:
    """Bind progress metadata for nested API-provider heartbeat emission."""
    merged = current_llm_context()
    if task_id is not _UNSET:
        merged["task_id"] = task_id
    if stage is not _UNSET:
        merged["stage"] = stage
    if label is not _UNSET:
        merged["label"] = label
    token = _LLM_CONTEXT.set(merged)
    try:
        yield dict(merged)
    finally:
        _LLM_CONTEXT.reset(token)


def subscribe_with_backlog(callback: Subscriber, *, after_id: int = 0) -> tuple[int, list[Event]]:
    """Subscribe and return buffered events with ``id > after_id``.

    The subscribe+snapshot operation is atomic under one lock, so callers
    can replay a consistent tail and then continue with live delivery.
    """
    global _NEXT_TOKEN
    try:
        last_seen = max(0, int(after_id or 0))
    except Exception:
        last_seen = 0
    with _LOCK:
        token = _NEXT_TOKEN
        _NEXT_TOKEN += 1
        _SUBSCRIBERS[token] = callback
        backlog = [dict(event) for event in _EVENT_HISTORY if int(event.get("id") or 0) > last_seen]
    return token, backlog


def events_since(after_id: int = 0) -> list[Event]:
    """Return buffered events with ``id > after_id`` (snapshot copy)."""
    try:
        last_seen = max(0, int(after_id or 0))
    except Exception:
        last_seen = 0
    with _LOCK:
        return [dict(event) for event in _EVENT_HISTORY if int(event.get("id") or 0) > last_seen]


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
    """Internal: reset subscriber and event state for isolated tests."""
    global _NEXT_TOKEN, _NEXT_EVENT_ID
    with _LOCK:
        _SUBSCRIBERS.clear()
        _EVENT_HISTORY.clear()
        _NEXT_TOKEN = 1
        _NEXT_EVENT_ID = 1
    _LLM_CONTEXT.set(None)
