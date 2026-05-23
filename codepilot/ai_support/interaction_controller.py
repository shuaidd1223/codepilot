"""Shared interaction-controller skeleton for chat/webui/go entrypoints.

This module intentionally stays small and side-effect free:
- parse user input prefixes
- decide intent with a consistent fallback strategy
"""

from __future__ import annotations

from typing import Callable, Optional

_VALID_INTENTS = {"question", "task", "requirement", "command"}


def _normalize_text(text: str) -> str:
    """Normalize a free-text requirement into a planner-friendly single line."""
    text = (text or "").strip()
    text = " ".join(text.split())
    if len(text) > 200:
        text = text[:200].rsplit(" ", 1)[0] + "..."
    return text


def parse_intent_prefix(text: str) -> tuple[Optional[str], str]:
    """Return (forced_intent, stripped_text)."""
    if not text:
        return None, text
    normalized = str(text or "").strip()
    if not normalized:
        return None, normalized
    first, rest = normalized[0], normalized[1:].lstrip()
    if first == "?" and rest:
        return "question", rest
    if first == "!" and rest:
        return "task", rest
    if first == "#" and rest:
        return "requirement", rest
    word_prefixes = (
        ("问题", "question"),
        ("问答", "question"),
        ("提问", "question"),
        ("任务", "task"),
        ("需求", "requirement"),
    )
    for prefix, intent in word_prefixes:
        if normalized == prefix:
            return intent, ""
        if normalized.startswith(prefix):
            tail = normalized[len(prefix):]
            if tail.startswith((" ", "\t", "\n", "：", ":")):
                return intent, tail[1:].lstrip()
    return None, text


def resolve_turn_intent(
    text: str,
    *,
    category: str = "auto",
    forced_intent: Optional[str] = None,
    classify_fn: Optional[Callable[..., str]] = None,
    classify_kwargs: Optional[dict] = None,
    fallback_intent: str = "requirement",
) -> str:
    """Resolve intent using forced/category overrides, then classifier, then fallback."""
    if forced_intent in _VALID_INTENTS:
        return forced_intent or "requirement"

    normalized_category = (category or "auto").strip().lower()
    if normalized_category in _VALID_INTENTS:
        return normalized_category

    fallback = fallback_intent if fallback_intent in _VALID_INTENTS else "requirement"
    if not classify_fn:
        return fallback

    try:
        classify_kwargs = classify_kwargs or {}
        resolved = (classify_fn(text, **classify_kwargs) or "").strip().lower()
    except Exception:
        return fallback
    return resolved if resolved in _VALID_INTENTS else fallback


WORKFLOW_PHASES = ("intake", "plan", "question", "command", "done", "error")


def build_workflow_session_record(
    *,
    phase: str,
    intent: str = "",
    next_action: str = "none",
) -> dict:
    """Build a standardized workflow-session record shared by CLI/WebUI entry points."""
    return {
        "phase": phase,
        "intent": intent or "",
        "next_action": next_action or "none",
    }

