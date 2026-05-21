"""Session message parsing and history helpers for Web UI actions."""

from __future__ import annotations

import json

from codepilot.ai_support.clarification_protocol import normalize_text


def _message_metadata(message: dict) -> dict:
    raw = message.get("metadata")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_task_ids(message: dict) -> list[int]:
    raw = message.get("task_ids")
    if isinstance(raw, list):
        return [int(item) for item in raw if str(item).isdigit()]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(parsed, list):
            ids: list[int] = []
            for item in parsed:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    ids.append(value)
            return ids
    return []


def _compact_session_text(value: str, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _session_history_turns(messages: list[dict], *, limit: int = 8) -> list[dict]:
    turns: list[dict] = []
    pending_user = ""
    for message in messages:
        role = message.get("role")
        content = _compact_session_text(message.get("content") or "", limit=260)
        if not content:
            continue
        if role == "user":
            if pending_user:
                turns.append({"user": pending_user, "assistant": ""})
            pending_user = content
        elif role == "assistant":
            if pending_user:
                turns.append({"user": pending_user, "assistant": content})
                pending_user = ""
            else:
                turns.append({"user": "", "assistant": content})
    if pending_user:
        turns.append({"user": pending_user, "assistant": ""})
    return turns[-limit:]


def _session_context_block(messages: list[dict], *, limit: int = 8, max_chars: int = 1600) -> str:
    relevant = [msg for msg in messages if str(msg.get("content") or "").strip()]
    if not relevant:
        return ""
    lines = []
    for message in relevant[-limit:]:
        role = "用户" if message.get("role") == "user" else "助手"
        intent = message.get("intent") or "-"
        task_ids = _message_task_ids(message)
        task_suffix = f" tasks={task_ids}" if task_ids else ""
        content = _compact_session_text(message.get("content") or "", limit=260)
        lines.append(f"- {role} [{intent}{task_suffix}]: {content}")
    block = "\n".join(lines)
    if len(block) <= max_chars:
        return block
    return block[-max_chars:].lstrip()


def _augment_text_with_session_context(text: str, session_context: str) -> str:
    text = normalize_text(text)
    context = str(session_context or "").strip()
    if not context:
        return text
    if "## 会话上下文" in text:
        return text
    return (
        f"{text}\n\n"
        "## 会话上下文（用于保持连续需求/问题的记忆）\n"
        f"{context}\n\n"
        "请把“当前输入”作为最新指令；如果它引用了上文、上一需求、刚才的计划或已有任务，"
        "必须结合上面的会话上下文理解。"
    )


def _session_message_payload(message: dict | None) -> dict:
    if not message:
        return {}
    parsed = dict(message)
    if parsed.get("task_ids"):
        try:
            parsed["task_ids"] = json.loads(parsed["task_ids"])
        except (json.JSONDecodeError, TypeError):
            parsed["task_ids"] = []
    else:
        parsed["task_ids"] = []
    if parsed.get("metadata"):
        try:
            parsed["metadata"] = json.loads(parsed["metadata"])
        except (json.JSONDecodeError, TypeError):
            parsed["metadata"] = {}
    else:
        parsed["metadata"] = {}
    return parsed
