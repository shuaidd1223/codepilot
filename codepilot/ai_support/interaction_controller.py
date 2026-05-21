"""Shared interaction-controller skeleton for chat/webui/go entrypoints.

This module intentionally stays small and side-effect free:
- parse user input prefixes
- decide intent with a consistent fallback strategy
- detect whether a turn should continue pending clarification
- normalize clarification continuation outcomes
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from codepilot.ai_support.clarification_protocol import normalize_clarification_questions, normalize_text

_VALID_INTENTS = {"question", "task", "requirement", "command"}


def _normalize_text(text: str) -> str:
    return normalize_text(text)


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


def should_continue_pending_clarification(
    *,
    pending_state: Optional[dict],
    forced_intent: Optional[str],
    raw_text: str,
    category: str = "auto",
) -> bool:
    """Return whether this turn should be treated as an answer to pending clarification."""
    if not pending_state:
        return False
    if forced_intent:
        return False
    if (category or "auto").strip().lower() != "auto":
        return False
    return not (raw_text or "").startswith("?")


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


@dataclass(frozen=True)
class ClarificationTransition:
    """Normalized transition after one clarification continuation attempt."""

    status: str
    message: str = ""
    pending_state: Optional[dict] = None
    questions: tuple[dict, ...] = ()
    refined_title: str = ""


WORKFLOW_PHASES = ("intake", "clarify", "plan", "question", "command", "done", "error")


def build_workflow_session_record(
    *,
    phase: str,
    intent: str = "",
    clarify_status: Optional[dict] = None,
    next_action: str = "none",
) -> dict:
    """Build a standardized workflow-session record shared by CLI/WebUI entry points."""
    record: dict = {
        "phase": phase,
        "intent": intent or "",
        "next_action": next_action or "none",
    }
    if clarify_status:
        record["clarify_status"] = dict(clarify_status)
    return record


def interpret_clarification_outcome(
    outcome: Optional[dict],
    *,
    pending_state: Optional[dict] = None,
    fallback_title: str = "",
    default_error_message: str = "澄清评估失败。",
    normalize_text: Optional[Callable[[str], str]] = None,
) -> ClarificationTransition:
    """Normalize continue-pending-clarification output into one stable shape."""
    payload = outcome or {}
    base_pending = pending_state if isinstance(pending_state, dict) else {}
    status = payload.get("status")

    if status == "error":
        message = str(payload.get("message") or default_error_message)
        if payload.get("error_kind") == "interrupt":
            return ClarificationTransition(
                status="interrupt",
                message=message,
                pending_state=base_pending,
            )
        return ClarificationTransition(
            status="error",
            message=message,
            pending_state=base_pending,
        )

    if status == "needs_clarification":
        next_pending = payload.get("pending_state")
        if not isinstance(next_pending, dict):
            next_pending = base_pending
        raw_questions = payload.get("questions") or next_pending.get("last_questions") or []
        questions = tuple(normalize_clarification_questions(raw_questions))
        return ClarificationTransition(
            status="needs_clarification",
            pending_state=next_pending,
            questions=questions,
        )

    norm = normalize_text or _normalize_text
    refined = norm(
        payload.get("refined_title")
        or base_pending.get("original_title")
        or fallback_title
    )
    return ClarificationTransition(
        status="ready",
        pending_state=None,
        refined_title=refined,
    )

