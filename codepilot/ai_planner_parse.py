"""Automation planner result parsing helpers.

Keep validation, normalization and reporting logic out of ``ai.py`` so the
planner entrypoints can stay thin and testable.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from codepilot.ai_backlog_dedup import filter_duplicate_tasks

_GARBAGE_KEYWORDS_ZH = ("等待", "请提供", "请输入", "待用户", "请发送", "等待高层")
_GARBAGE_KEYWORDS_EN = (
    "awaiting",
    "waiting for",
    "please provide",
    "no goal",
    "user input needed",
    "awaiting-user",
)
_PLACEHOLDER_TITLES = {"awaiting-user-input", "waiting", "pending", "no-op", "placeholder"}

_REQ_STOPWORDS = {
    "the",
    "and",
    "for",
    "that",
    "with",
    "this",
    "from",
    "into",
    "can",
    "not",
    "but",
    "all",
    "will",
    "have",
    "are",
    "was",
    "then",
    "just",
    "one",
    "also",
    "use",
    "using",
    "some",
    "about",
    "what",
    "which",
    "how",
    "been",
    "more",
    "when",
}


@dataclass(frozen=True)
class PlannerResultEnvelope:
    """Unified intermediate representation for planner outputs.

    ``source`` distinguishes whether the caller passed a raw JSON string or an
    already-decoded dict. Downstream steps always consume ``payload`` only.
    """

    source: str
    payload: dict


def _coerce_planner_result(breakdown: dict | str) -> PlannerResultEnvelope:
    if isinstance(breakdown, str):
        try:
            payload = json.loads(breakdown)
        except json.JSONDecodeError as exc:
            raise RuntimeError("自动规划返回的内容不是有效 JSON，暂时无法继续自动规划。") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("自动规划返回的结果格式不正确，暂时无法继续自动规划。")
        return PlannerResultEnvelope(source="text_json", payload=payload)

    if isinstance(breakdown, dict):
        return PlannerResultEnvelope(source="structured", payload=breakdown)

    raise RuntimeError("自动规划返回的结果格式不正确，暂时无法继续自动规划。")


def _extract_raw_tasks(payload: dict) -> list[object]:
    raw_tasks = payload.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise RuntimeError("任务拆分结果为空")
    return raw_tasks


def _is_garbage_task(task_item: object) -> bool:
    if not isinstance(task_item, dict):
        return True
    title = (task_item.get("title") or "").strip()
    goal = (task_item.get("goal") or "").strip()
    combined = f"{title} {goal}"
    combined_lower = combined.lower()
    if any(keyword in combined for keyword in _GARBAGE_KEYWORDS_ZH):
        return True
    if any(keyword in combined_lower for keyword in _GARBAGE_KEYWORDS_EN):
        return True
    if title.lower() in _PLACEHOLDER_TITLES:
        return True
    return not title


def _build_all_filtered_error(payload: dict, raw_tasks: list[object]) -> RuntimeError:
    summary = str(payload.get("summary", ""))
    rejected = "\n".join(
        f"  - {(item.get('title') or '?')[:60]} :: {(item.get('goal') or '?')[:80]}"
        for item in raw_tasks[:3]
        if isinstance(item, dict)
    ) or "  - （无可展示任务）"
    return RuntimeError(
        f"规划器把这次需求理解成「等待 / 请用户补充」一类的占位任务，全部被过滤掉了。\n"
        f"规划器摘要：{summary[:200]}\n"
        f"被过滤的任务示例：\n{rejected}\n"
        f"通常出现在需求过于宽泛 / 探索性时（比如「看看有没有什么优化点」）。建议：\n"
        f"  1. 把需求写得更具体：指明要修改 / 新增 / 优化哪一块；\n"
        f"  2. 想让 AI 主动找改进点：用 `codepilot inspect -p <项目>`；\n"
        f"  3. 或换 codex 规划器（自带兜底降级），用 --planner codex 重试。"
    )


def _filter_and_normalize_tasks(payload: dict, raw_tasks: list[object]) -> list[dict]:
    valid_tasks = [dict(task_item) for task_item in raw_tasks if not _is_garbage_task(task_item)]
    if not valid_tasks:
        raise _build_all_filtered_error(payload, raw_tasks)
    if len(valid_tasks) < len(raw_tasks):
        dropped = len(raw_tasks) - len(valid_tasks)
        sys.stderr.write(f"  [planner] 过滤掉 {dropped} 个无效任务\n")
    return valid_tasks


def _warn_requirement_drift(title: str, tasks: list[dict]) -> None:
    req_words = set(re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]{3,}", title.lower()))
    req_words -= _REQ_STOPWORDS
    if not req_words:
        return

    for task_item in tasks:
        task_text = (
            (task_item.get("title") or "")
            + " "
            + (task_item.get("goal") or "")
            + " "
            + " ".join(task_item.get("files") or [])
        ).lower()
        task_words = set(re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]{3,}", task_text))
        if req_words & task_words:
            continue
        sys.stderr.write(
            f"  [planner] 警告: 任务 '{task_item.get('title', '')[:40]}' "
            f"与需求无明显关联, 可能跑偏\n"
        )


def _normalize_dedup_skipped(payload: dict) -> list[dict]:
    normalized: list[dict] = []
    for item in payload.get("dedup_skipped") or []:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "proposed_title": item.get("proposed_title"),
                "matched_existing_id": item.get("matched_existing_id"),
                "matched_existing_title": item.get("matched_existing_title"),
            }
        )
    return normalized


def _apply_existing_task_dedup(
    tasks: list[dict],
    *,
    existing_tasks: Optional[list[dict]],
    dedup_skipped: list[dict],
    progress_callback: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], list[dict]]:
    if not existing_tasks:
        return tasks, dedup_skipped

    kept_tasks, dropped_tasks = filter_duplicate_tasks(tasks, existing_tasks)
    if not dropped_tasks:
        return kept_tasks, dedup_skipped

    seen = {
        (item.get("proposed_title"), item.get("matched_existing_id"), item.get("matched_existing_title"))
        for item in dedup_skipped
    }

    for dup in dropped_tasks:
        msg = (
            f"  [planner] 跳过重复任务 '{(dup.get('title') or '')[:40]}' "
            f"（已存在 #{dup.get('_dedup_matched_id')} "
            f"'{(dup.get('_dedup_matched_title') or '')[:40]}'）"
        )
        sys.stderr.write(msg + "\n")
        if progress_callback:
            try:
                progress_callback(msg.strip())
            except Exception:
                pass

        normalized_dup = {
            "proposed_title": dup.get("title"),
            "matched_existing_id": dup.get("_dedup_matched_id"),
            "matched_existing_title": dup.get("_dedup_matched_title"),
        }
        dedup_key = (
            normalized_dup["proposed_title"],
            normalized_dup["matched_existing_id"],
            normalized_dup["matched_existing_title"],
        )
        if dedup_key not in seen:
            dedup_skipped.append(normalized_dup)
            seen.add(dedup_key)

    return kept_tasks, dedup_skipped


def _finalize_payload(payload: dict, *, tasks: list[dict], dedup_skipped: list[dict]) -> dict:
    normalized = dict(payload)
    normalized["tasks"] = tasks
    normalized["complexity"] = normalized.get("complexity") or ("simple" if len(tasks) <= 1 else "complex")
    normalized["should_split"] = bool(
        normalized.get("should_split")
        if normalized.get("should_split") is not None
        else len(tasks) > 1
    )
    if dedup_skipped:
        normalized["dedup_skipped"] = dedup_skipped
    else:
        normalized.pop("dedup_skipped", None)
    return normalized


def parse_automation_planner_result(
    breakdown: dict | str,
    *,
    title: str,
    max_tasks: int = 5,
    existing_tasks: Optional[list[dict]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Normalize, validate and deduplicate automation planner output."""
    envelope = _coerce_planner_result(breakdown)
    raw_tasks = _extract_raw_tasks(envelope.payload)
    valid_tasks = _filter_and_normalize_tasks(envelope.payload, raw_tasks)
    _warn_requirement_drift(title, valid_tasks)

    max_tasks = max(1, min(max_tasks, 8))
    capped_tasks = valid_tasks[:max_tasks]
    dedup_skipped = _normalize_dedup_skipped(envelope.payload)
    capped_tasks, dedup_skipped = _apply_existing_task_dedup(
        capped_tasks,
        existing_tasks=existing_tasks,
        dedup_skipped=dedup_skipped,
        progress_callback=progress_callback,
    )
    return _finalize_payload(envelope.payload, tasks=capped_tasks, dedup_skipped=dedup_skipped)

