"""Feishu inbound event and card-action parsing helpers."""

from __future__ import annotations

from typing import Any


def inbound_dedupe_key(payload: dict[str, Any]) -> str:
    event_id = str(payload.get("event_id") or "").strip()
    if event_id:
        return f"event:{event_id}"
    message_id = str(payload.get("message_id") or "").strip()
    if message_id:
        return f"msg:{message_id}"
    return ""


def card_action_event(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    return event if isinstance(event, dict) else {}


def card_action_chat_id(event: dict[str, Any]) -> str:
    context = event.get("context") if isinstance(event.get("context"), dict) else {}
    return str(
        event.get("chat_id")
        or event.get("open_chat_id")
        or context.get("open_chat_id")
        or context.get("chat_id")
        or ""
    ).strip()


def card_action_command(event: dict[str, Any]) -> str:
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    value = action.get("value") if isinstance(action.get("value"), dict) else {}
    for key in ("command", "cmd", "text"):
        command = str(value.get(key) or "").strip()
        if command:
            return command
    return ""
