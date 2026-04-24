"""Shared quality-gate helpers for task planning and inspection.

Both the planner (``auto_workflow._evaluate_planning_quality``) and the
inspector (``inspect._filter_candidates``) want to reject "filler" tasks:
titles/goals that are pure placeholder noise. They previously kept
independent keyword tuples that drifted apart.

This module centralizes:

* the baseline filler vocabulary (Chinese + obvious English)
* a planner-specific extension (can be stricter because user-provided
  task titles are unlikely to contain bare English markers like "todo")
* an inspect-specific extension (deliberately avoids "todo" / "placeholder"
  because those appear legitimately in inspect's signal references)
* ``looks_generic(text, keywords)`` — case-insensitive substring check
* ``evidence_grounded_in(evidence, tokens)`` — cheap two-way substring
  match with a minimum token length, shared by inspect's signal check
  and any future planner-side evidence gate.
"""

from __future__ import annotations

from typing import Iterable


# Common filler phrases that apply to both planner and inspector. Lowercase
# here but matching is case-insensitive at call time.
BASE_FILLER_KEYWORDS: tuple[str, ...] = (
    "补一下文档",
    "补充文档",
    "加日志",
    "加点日志",
    "通用优化",
    "泛化",
    "待补充",
    "杂项优化",
    "其它优化",
    "其他优化",
    "简单优化",
    "小优化",
    "随手优化",
    "整体重构",
    "全面梳理",
)


# Planner-specific: task titles/goals in the planning stage should never
# literally include bare placeholder markers; these are red flags that the
# LLM is stalling. Safe to include English words here because user-supplied
# requirements don't tend to contain them verbatim.
PLANNER_FILLER_KEYWORDS: tuple[str, ...] = BASE_FILLER_KEYWORDS + (
    "占位",
    "placeholder",
    "pending",
    "awaiting",
    "misc",
    "todo",
)


# Inspect-specific: the inspector's signals LEGITIMATELY reference TODO
# markers in code, so we must NOT treat bare "todo" as filler. We also
# tighten some Chinese phrases to avoid false positives on real fixes.
INSPECT_FILLER_KEYWORDS: tuple[str, ...] = BASE_FILLER_KEYWORDS + (
    "占位任务",
)


def looks_generic(text: str, keywords: Iterable[str]) -> bool:
    """Return True if any keyword appears as a case-insensitive substring.

    ``text`` may be the concatenation of title + goal (or any other free-text
    field). Empty text always returns False.
    """
    if not text:
        return False
    haystack = text.lower()
    for keyword in keywords:
        if not keyword:
            continue
        if keyword.lower() in haystack:
            return True
    return False


def evidence_grounded_in(
    evidence: str,
    tokens: Iterable[str],
    *,
    min_token_length: int = 4,
) -> bool:
    """Cheap containment check shared by evidence gates.

    Returns True iff ``evidence`` overlaps with any non-trivial ``token``
    (tokens shorter than ``min_token_length`` are ignored to avoid accidental
    matches on 1–3 character fragments like "api" or "T1"). The match is
    bidirectional so both short evidence citing a long token and long
    evidence summarizing a short canonical token work.
    """
    if not evidence:
        return False
    ev = evidence.strip()
    if not ev:
        return False
    ev_lower = ev.lower()

    for token in tokens:
        if not token:
            continue
        token_stripped = token.strip()
        if len(token_stripped) < min_token_length:
            continue
        token_lower = token_stripped.lower()
        if token_lower in ev_lower or ev_lower in token_lower:
            return True
    return False
