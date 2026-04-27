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

from dataclasses import dataclass
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


# Inspect-specific: the inspector's signals legitimately reference
# task-marker comments in code, so we must NOT treat bare "todo" as filler. We also
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


@dataclass(slots=True)
class PlannerQualitySummary:
    total_tasks: int = 0
    placeholder_like_tasks: int = 0
    title_missing_count: int = 0
    goal_missing_count: int = 0
    ac_missing_count: int = 0
    files_missing_count: int = 0
    evidence_missing_count: int = 0


def _normalize_text_list(raw: object) -> list[str]:
    return [
        str(item).strip()
        for item in (raw if isinstance(raw, list) else [])
        if str(item).strip()
    ]


def evaluate_planner_task(
    task: object,
    *,
    index: int,
    keywords: Iterable[str] = PLANNER_FILLER_KEYWORDS,
) -> tuple[list[str], PlannerQualitySummary]:
    """Evaluate one planner task and return advisory messages + counters."""
    advisory: list[str] = []
    summary = PlannerQualitySummary(total_tasks=1)

    if not isinstance(task, dict):
        return [f"Task {index} is not an object."], summary

    task_title = str(task.get("title") or "").strip()
    goal = str(task.get("goal") or "").strip()
    acceptance = _normalize_text_list(task.get("acceptance_criteria"))
    files = _normalize_text_list(task.get("files"))

    if not task_title:
        summary.title_missing_count += 1
        advisory.append(f"Task {index} is missing `title`.")

    if not goal:
        summary.goal_missing_count += 1
        advisory.append(f"Task {index} is missing `goal`.")

    if looks_generic(" ".join([task_title, goal]), keywords):
        summary.placeholder_like_tasks += 1
        advisory.append(f"Task {index} looks placeholder-like: `{task_title or 'unnamed task'}`.")

    if not acceptance:
        summary.ac_missing_count += 1
        advisory.append(f"Task {index} is missing `acceptance_criteria` entries.")

    if not files:
        summary.files_missing_count += 1
        advisory.append(f"Task {index} is missing `files` entries.")

    evidence = str(task.get("evidence") or "").strip()
    if not evidence:
        summary.evidence_missing_count += 1
        advisory.append(
            f"Task {index} has empty `evidence` — planner should cite a recon "
            "finding / file / existing task."
        )

    return advisory, summary


def merge_planner_quality_summaries(
    left: PlannerQualitySummary,
    right: PlannerQualitySummary,
) -> PlannerQualitySummary:
    return PlannerQualitySummary(
        total_tasks=left.total_tasks + right.total_tasks,
        placeholder_like_tasks=left.placeholder_like_tasks + right.placeholder_like_tasks,
        title_missing_count=left.title_missing_count + right.title_missing_count,
        goal_missing_count=left.goal_missing_count + right.goal_missing_count,
        ac_missing_count=left.ac_missing_count + right.ac_missing_count,
        files_missing_count=left.files_missing_count + right.files_missing_count,
        evidence_missing_count=left.evidence_missing_count + right.evidence_missing_count,
    )


def planner_quality_blocking_messages(summary: PlannerQualitySummary) -> list[str]:
    """Return blocking repair signals for obvious planner-template failures."""
    blocking: list[str] = []
    total = summary.total_tasks
    if total <= 0:
        return blocking
    if summary.placeholder_like_tasks == total:
        blocking.append("All tasks look placeholder-like and lack concrete deliverables.")
    if summary.title_missing_count == total:
        blocking.append("All tasks are missing `title`.")
    if summary.goal_missing_count == total:
        blocking.append("All tasks are missing `goal`.")
    if summary.ac_missing_count == total:
        blocking.append("All tasks are missing `acceptance_criteria` entries.")
    if summary.files_missing_count == total:
        blocking.append("All tasks are missing `files` entries.")
    if summary.evidence_missing_count == total:
        blocking.append(
            "All tasks have empty `evidence` — planner appears to be fabricating tasks."
        )
    return blocking


def evaluate_planning_breakdown(title: str, breakdown: dict) -> tuple[list[str], list[str]]:
    """Rough structural checks only (format/template), no semantic hard gates."""
    del title
    tasks = breakdown.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return ["No executable tasks were produced."], []

    advisory: list[str] = []
    summary = PlannerQualitySummary()

    for idx, task in enumerate(tasks, 1):
        task_advisory, task_summary = evaluate_planner_task(task, index=idx)
        advisory.extend(task_advisory)
        summary = merge_planner_quality_summaries(summary, task_summary)

    should_split = breakdown.get("should_split")
    if isinstance(should_split, bool):
        if should_split and len(tasks) <= 1:
            advisory.append("`should_split=true` but only one task was produced.")
        if (not should_split) and len(tasks) > 1:
            advisory.append("`should_split=false` but multiple tasks were produced.")

    return planner_quality_blocking_messages(summary), advisory
