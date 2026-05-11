"""Requirement assessment (deprecated — AI agent handles clarification).

The old hardcoded clarification layer (heuristic + AI prompt + multi-turn loop)
has been removed. All requirement understanding is now delegated to the AI
agent (codex / opencode). This module only provides a pass-through so existing
import paths do not break.

Public API:

* :func:`assess_requirement` - always returns ``{"status": "ready", ...}``.
* :func:`merge_clarification_history` - preserved for backward compatibility.
"""

from __future__ import annotations

from typing import Callable, Optional

from codepilot.ai_support.clarification_protocol import (
    build_clarification_answer_summary,
)


def merge_clarification_history(original: str, qa_history: list[dict]) -> str:
    """Fold Q/A rounds back into a single refined prompt for the planner."""
    parts = [original.strip()]
    for round_ in qa_history or []:
        answer_entries = round_.get("answers") if isinstance(round_.get("answers"), list) else []
        a = build_clarification_answer_summary(answer_entries) or (round_.get("answer") or "").strip()
        if not a:
            continue
        questions = round_.get("questions") if isinstance(round_.get("questions"), list) else []
        q_parts = []
        for item in questions:
            text = item.get("text") if isinstance(item, dict) else str(item)
            if text:
                q_parts.append(str(text))
        q = " | ".join(q_parts)
        if q:
            parts.append(f"补充（问：{q}；答：{a}）")
        else:
            parts.append(f"补充：{a}")
    return " / ".join(p for p in parts if p)


def assess_requirement(
    title: str,
    *,
    project_path: str = "",
    config_ref: str = "",
    qa_history: Optional[list[dict]] = None,
    max_turns: int = 3,
    classifier_provider: str = "",
    classifier_model: str = "",
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    planner: str = "codex",
    timeout: int = 45,
    stream_callback: Callable[[str], None] | None = None,
) -> dict:
    """直接返回 ready——由 AI 智能体接管需求澄清，跳过系统硬编码澄清层。"""
    merged = merge_clarification_history(title, qa_history)
    return {
        "status": "ready",
        "refined_title": merged,
        "source": "passthrough",
        "reason": "AI 智能体接管需求理解，跳过系统硬编码澄清层",
    }
