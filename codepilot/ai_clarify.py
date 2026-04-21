"""Multi-turn requirement clarification.

The planner used to hard-split any input the moment it arrived, even when the
user's wording was too vague ("优化一下", "看看能不能改进下性能") to yield
meaningful subtasks. This module introduces a lightweight clarification loop:

* a cheap heuristic flags obviously-vague inputs;
* an AI follow-up (only when heuristic fires) asks 2-3 concrete questions
  *grounded in the project context* so the questions don't feel generic;
* the chat session accumulates the user's answers and re-asks the AI whether
  the refined requirement is concrete enough to plan, up to a few turns.

Public API:

* :func:`assess_requirement` - single entry point; returns either
  ``{"status": "ready", "refined_title": ...}`` or
  ``{"status": "needs_clarification", "questions": [...]}``.

* :func:`merge_clarification_history` - convenience to fold the Q&A history
  back into a single refined requirement string to hand to the planner.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

from codepilot.ai_planner_context import collect_planner_context
from codepilot.prompts import load_prompt as _load_prompt


# Words that *by themselves* signal intent but without a concrete object.
# "优化性能" is specific; bare "优化" alone is not.
_VAGUE_VERBS_ZH = (
    "优化", "重构", "改进", "完善", "改造", "改善", "提升",
    "整理", "清理", "整顿", "整合", "梳理",
)
_VAGUE_VERBS_EN = (
    "improve", "optimize", "refactor", "cleanup", "clean up",
    "enhance", "polish", "tidy",
)

# Concrete object markers — when present alongside a vague verb we accept
# the requirement as actionable (e.g. "优化 webui 加载速度" has "webui").
_CONCRETE_HINTS = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.\-/]{2,}"  # English identifier or path
    r"|\b\d+\b"                        # a literal number / metric
    r"|[\u4e00-\u9fff]+\.(?:py|ts|tsx|js|md|vue|go|rs|java)"  # CJK+extension (rare)
)

_EXPLICIT_OBJECT_HINTS = (
    "模块", "页面", "接口", "命令", "脚本", "测试", "数据库", "配置", "服务",
    "组件", "函数", "类", "路由", "任务", "列表", "表单", "流程", "字段",
    "api", "cli", "webui", "sdk",
)


CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ready", "needs_clarification"],
        },
        "reason": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 4,
        },
        "refined_title": {"type": "string"},
    },
    "required": ["status"],
    "additionalProperties": False,
}


# Template body lives in codepilot/prompts/clarify.md.
CLARIFY_PROMPT_TEMPLATE = _load_prompt("clarify")


def heuristic_needs_clarification(title: str) -> bool:
    """Cheap pre-filter. Return True when the input is obviously vague."""
    t = (title or "").strip()
    if not t:
        return False

    # Very short — suspicious. Count English words generously (each
    # meaningful token is worth more than a CJK char), then flag if the
    # combined signal is very low.
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", t)
    alpha_words = re.findall(r"[A-Za-z]{2,}", t)
    total_signal = len(cjk_chars) + 2 * len(alpha_words)
    if total_signal < 4:
        return True

    low = t.lower()
    hits_vague = any(w in t for w in _VAGUE_VERBS_ZH) or any(w in low for w in _VAGUE_VERBS_EN)
    if not hits_vague:
        return False

    # Has a vague verb — check whether a concrete object is attached.
    if _CONCRETE_HINTS.search(t):
        return False

    # Vague verb, no concrete English/path/number marker: flag for the AI to
    # decide. The AI can still choose "ready" if the Chinese context is
    # actually concrete enough (e.g. a domain noun the heuristic doesn't
    # recognize), so we err on the side of asking one cheap extra call.
    return True


def merge_clarification_history(original: str, qa_history: list[dict]) -> str:
    """Fold Q/A rounds back into a single refined prompt for the planner."""
    parts = [original.strip()]
    for round_ in qa_history:
        q = (round_.get("question") or "").strip()
        a = (round_.get("answer") or "").strip()
        if not a:
            continue
        if q:
            parts.append(f"补充（问：{q}；答：{a}）")
        else:
            parts.append(f"补充：{a}")
    return " / ".join(p for p in parts if p)


def _looks_explicitly_concrete(title: str) -> bool:
    """Return whether the requirement already names a concrete object/scope."""
    t = (title or "").strip()
    if not t:
        return False
    if _CONCRETE_HINTS.search(t):
        return True
    low = t.lower()
    return any(hint in t or hint in low for hint in _EXPLICIT_OBJECT_HINTS)


def _strip_json_envelope(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start : end + 1]
    return raw.strip()


def _invoke_clarifier_ai(
    prompt: str,
    *,
    classifier_provider: str,
    classifier_model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    project_path: str,
    config_ref: str,
    planner: str,
    timeout: int,
) -> dict:
    """Route through the unified AI gateway (API → local CLI fallback)."""
    from codepilot.ai_gateway import GatewayRequest, call_structured

    response = call_structured(
        GatewayRequest(
            prompt=prompt,
            schema=CLARIFY_SCHEMA,
            classifier_provider=classifier_provider,
            classifier_model=classifier_model,
            api_key=api_key,
            base_url=base_url,
            project_path=project_path,
            config_ref=config_ref,
            planner=planner or "codex",
            timeout=timeout,
        )
    )
    if not response.ok:
        raise RuntimeError(response.error or "clarifier AI call failed")
    return response.payload or {}


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
) -> dict:
    """Decide whether the requirement is concrete enough to plan.

    Args:
        title: The *original* requirement the user typed.
        project_path: Used to fetch project context for grounded questions.
        qa_history: List of ``{"question": str, "answer": str}`` rounds so far.
        max_turns: Safety cap — after this many rounds we force ``ready``.

    Returns:
        ``{"status": "ready", "refined_title": str, "source": "heuristic|ai|forced"}``
        or
        ``{"status": "needs_clarification", "questions": [str, ...], "source": "ai", "turn": int}``.
    """
    qa_history = list(qa_history or [])
    turn = len(qa_history) + 1

    # Cheap guard: merged requirement with enough user input → accept.
    merged = merge_clarification_history(title, qa_history)
    if turn > max_turns:
        return {
            "status": "ready",
            "refined_title": merged,
            "source": "forced",
            "reason": f"澄清轮次超过 {max_turns}，按当前信息规划",
        }

    # First round: if the requirement is both concise and concrete, skip AI.
    # Otherwise, let AI perform a contextual adequacy check (it may still
    # return ready immediately without follow-up questions).
    if not qa_history and not heuristic_needs_clarification(title):
        if _looks_explicitly_concrete(title):
            return {
                "status": "ready",
                "refined_title": title.strip(),
                "source": "heuristic",
                "reason": "需求已包含明确作用对象，可直接规划",
            }

    # Otherwise, consult the AI.
    context = collect_planner_context(project_path, merged, max_chars=2500)
    history_lines = []
    for idx, round_ in enumerate(qa_history, 1):
        q = (round_.get("question") or "").strip()
        a = (round_.get("answer") or "").strip()
        if q:
            history_lines.append(f"Q{idx}: {q}")
        if a:
            history_lines.append(f"A{idx}: {a}")
    history_block = "\n".join(history_lines) if history_lines else "（第一次对话，无前置问答）"

    prompt = CLARIFY_PROMPT_TEMPLATE.format(
        history_block=history_block,
        title=merged,
        context=context or "（无项目上下文）",
    )

    try:
        payload = _invoke_clarifier_ai(
            prompt,
            classifier_provider=classifier_provider,
            classifier_model=classifier_model,
            api_key=api_key,
            base_url=base_url,
            project_path=project_path,
            config_ref=config_ref,
            planner=planner,
            timeout=timeout,
        )
    except Exception as exc:
        # On AI failure we fall back to "ready" so the user isn't stuck.
        from codepilot.logger import get_logger

        get_logger("clarifier").warning(
            "clarifier AI failed; falling back to planner with merged requirement: %s", exc
        )
        sys.stderr.write(f"  [clarifier] 判定失败，按原需求继续规划：{exc}\n")
        return {
            "status": "ready",
            "refined_title": merged,
            "source": "ai-error",
            "reason": f"clarifier 调用失败：{exc}",
        }

    status = (payload.get("status") or "").strip()
    if status == "needs_clarification":
        questions = [q.strip() for q in (payload.get("questions") or []) if q and q.strip()]
        questions = questions[:3]
        if not questions:
            return {
                "status": "ready",
                "refined_title": merged,
                "source": "ai",
                "reason": "clarifier 未提供问题，按现有信息规划",
            }
        return {
            "status": "needs_clarification",
            "questions": questions,
            "source": "ai",
            "turn": turn,
            "reason": (payload.get("reason") or "").strip(),
        }

    refined = (payload.get("refined_title") or merged).strip() or merged
    return {
        "status": "ready",
        "refined_title": refined,
        "source": "ai",
        "reason": (payload.get("reason") or "").strip(),
    }
