"""Requirement assessment helpers.

Public API:

* :func:`assess_requirement` - legacy clarification assessment used by direct
  clarify callsites.
* :func:`merge_clarification_history` - preserved for backward compatibility.
"""

from __future__ import annotations

from typing import Callable, Optional

from codepilot.ai_support.clarification_protocol import (
    build_clarification_answer_summary,
    normalize_clarification_questions,
)
from codepilot.core.config import load_project_config
from codepilot.gateway.types import GatewayRequest
from codepilot.prompts import load_prompt


_CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "needs_clarification"]},
        "refined_title": {"type": "string"},
        "reason": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["status"],
}


def heuristic_needs_clarification(title: str) -> bool:
    """Return whether a requirement is too vague for direct planning."""
    text = " ".join(str(title or "").strip().split())
    lower = text.lower()
    if not text:
        return True
    specific_markers = (
        "/",
        ".py",
        ".ts",
        ".js",
        "webui",
        "sidebar",
        "chat",
        "cli",
        "dark mode",
        "启动速度",
        "会话历史",
        "组件化",
        "多轮澄清",
    )
    if any(marker in lower for marker in specific_markers) or any(marker in text for marker in specific_markers):
        return False
    vague_exact = {"优化", "优化一下", "修 bug", "修bug", "optimize"}
    if lower in vague_exact or text in vague_exact:
        return True
    vague_terms = ("优化", "改进", "提升", "修复", "bug", "问题", "用户体验")
    if any(term in lower for term in vague_terms) or any(term in text for term in vague_terms):
        return True
    return False


def _looks_specific_enough(title: str) -> bool:
    text = str(title or "").strip()
    lower = text.lower()
    if heuristic_needs_clarification(text):
        return False
    concrete_markers = (
        "/",
        ".py",
        ".ts",
        ".js",
        "webui",
        "sidebar",
        "chat",
        "cli",
        "dark mode",
        "启动速度",
        "会话历史",
        "组件化",
        "多轮澄清",
    )
    return any(marker in lower for marker in concrete_markers) or any(marker in text for marker in concrete_markers)


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


def _history_block(qa_history: Optional[list[dict]]) -> str:
    if not qa_history:
        return "(none)"
    lines: list[str] = []
    for idx, item in enumerate(qa_history, 1):
        if not isinstance(item, dict):
            continue
        question = item.get("question") or item.get("questions") or ""
        answer = item.get("answer") or build_clarification_answer_summary(item.get("answers") or [])
        lines.append(f"{idx}. Q: {question}\n   A: {answer}")
    return "\n".join(lines) if lines else "(none)"


def _agent_language_for_ref(project_path: str = "", config_ref: str = "") -> str:
    cfg = load_project_config(config_ref or project_path)
    if cfg and getattr(cfg, "automation", None):
        return str(getattr(cfg.automation, "agent_language", "en") or "en")
    return "en"


def _invoke_clarifier_ai(
    title: str,
    *,
    project_path: str = "",
    config_ref: str = "",
    qa_history: Optional[list[dict]] = None,
    classifier_provider: str = "",
    classifier_model: str = "",
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    planner: str = "codex",
    timeout: int = 45,
    stream_callback: Callable[[str], None] | None = None,
) -> dict:
    """Ask the configured gateway whether clarification is needed."""
    from codepilot.gateway import service as ai_gateway

    prompt = load_prompt("clarify", language=_agent_language_for_ref(project_path, config_ref)).format(
        title=title,
        history_block=_history_block(qa_history),
        context=f"Project path: {project_path or '(not provided)'}",
    )
    response = ai_gateway.call_structured(
        GatewayRequest(
            prompt=prompt,
            schema=_CLARIFY_SCHEMA,
            classifier_provider=classifier_provider,
            classifier_model=classifier_model,
            api_key=api_key,
            base_url=base_url,
            project_path=project_path,
            config_ref=config_ref,
            planner=planner,
            timeout=timeout,
            stream_callback=stream_callback,
        )
    )
    if not response.ok:
        raise RuntimeError(response.error or "clarifier gateway failed")
    return dict(response.payload or {})


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
    """Assess whether a requirement is ready or needs a focused clarification."""
    merged = merge_clarification_history(title, qa_history)
    turn = len(qa_history or []) + 1
    if len(qa_history or []) >= max_turns:
        return {
            "status": "ready",
            "refined_title": merged,
            "source": "forced",
            "reason": "maximum clarification turns reached",
        }

    if _looks_specific_enough(merged):
        return {
            "status": "ready",
            "refined_title": merged,
            "source": "heuristic",
            "reason": "requirement already names a concrete target or behavior",
        }

    try:
        payload = _invoke_clarifier_ai(
            merged,
            project_path=project_path,
            config_ref=config_ref,
            qa_history=qa_history,
            classifier_provider=classifier_provider,
            classifier_model=classifier_model,
            api_key=api_key,
            base_url=base_url,
            planner=planner,
            timeout=timeout,
            stream_callback=stream_callback,
        )
    except Exception as exc:
        return {
            "status": "ready",
            "refined_title": merged,
            "source": "ai-error",
            "reason": str(exc),
        }

    if payload.get("status") == "needs_clarification":
        return {
            "status": "needs_clarification",
            "questions": normalize_clarification_questions(payload.get("questions") or []),
            "source": "ai",
            "turn": turn,
            "reason": payload.get("reason", ""),
        }

    return {
        "status": "ready",
        "refined_title": str(payload.get("refined_title") or merged).strip() or merged,
        "source": "ai",
        "reason": payload.get("reason", ""),
    }
