"""Backlog-aware planning: feed existing open tasks to the planner and drop
near-duplicates the planner proposes anyway.

The planner used to be blind to the current project backlog, so asking "再加一
下 session sidebar" when a ``给 webui 加会话历史 sidebar`` task was already in
``backlog`` would create a second, redundant task. This module:

1. Formats open tasks (``backlog`` / ``in_progress``) into a block injected
   into the planner prompt, so the planner can proactively avoid duplicates
   or annotate an extension.
2. Post-filters the planner's output: any new task whose title is too close
   to an existing open task (``difflib.SequenceMatcher`` ratio ≥ threshold)
   is dropped, and the dropped info is surfaced via a progress callback.

The module deliberately takes ``existing_tasks`` as an argument instead of
reaching into the DB itself, so unit tests don't need a DB fixture.
"""

from __future__ import annotations

import difflib
import re
from typing import Iterable, Optional

# When planner output and an existing task share ≥ this much of a title,
# treat them as duplicates. Tuned for short bilingual titles; 0.78 is
# forgiving enough to merge "给 webui 加 sidebar" with "webui 加 sidebar"
# but lets distinct cousins through.
_DEDUP_THRESHOLD = 0.78


def _normalize_title(title: str) -> str:
    """Strip punctuation, lowercase, and collapse whitespace for matching."""
    if not title:
        return ""
    # Drop ASCII and common CJK punctuation but keep alphanumeric + CJK glyphs.
    cleaned = re.sub(r"[\s，。！？,.!?;:；：/\\_\-()\[\]（）【】「」『』\"'`]+", " ", title)
    return cleaned.strip().lower()


def _title_similarity(a: str, b: str) -> float:
    """Return a 0..1 similarity score across normalized titles."""
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def format_existing_block(existing_tasks: Optional[Iterable[dict]]) -> str:
    """Render an existing-tasks block to inject into the planner prompt.

    Only ``backlog`` / ``in_progress`` tasks are surfaced (done / cancelled /
    failed are intentionally hidden — planner should not try to link to them).
    """
    if not existing_tasks:
        return "（项目中暂无未完成任务）"

    rows: list[str] = []
    for task in existing_tasks:
        status = (task.get("status") or "").strip()
        if status not in {"backlog", "in_progress"}:
            continue
        tid = task.get("id")
        title = (task.get("title") or "").strip()
        if not title:
            continue
        rows.append(f"- #{tid} [{status}] {title}")
        if len(rows) >= 30:
            break
    return "\n".join(rows) if rows else "（项目中暂无未完成任务）"


def find_duplicate_match(
    candidate_title: str,
    existing_tasks: Iterable[dict],
    *,
    threshold: float = _DEDUP_THRESHOLD,
) -> Optional[dict]:
    """Return the best-scoring existing task that looks like a duplicate, if any."""
    best: tuple[float, Optional[dict]] = (0.0, None)
    for task in existing_tasks:
        status = (task.get("status") or "").strip()
        if status not in {"backlog", "in_progress"}:
            continue
        score = _title_similarity(candidate_title, task.get("title") or "")
        if score > best[0]:
            best = (score, task)
    if best[0] >= threshold:
        return best[1]
    return None


def filter_duplicate_tasks(
    planner_tasks: list[dict],
    existing_tasks: Iterable[dict],
    *,
    threshold: float = _DEDUP_THRESHOLD,
) -> tuple[list[dict], list[dict]]:
    """Split planner output into (kept, dropped-duplicates).

    Each dropped entry carries ``_dedup_matched_id`` / ``_dedup_matched_title``
    so callers (and the chat UI) can explain to the user that the requirement
    is already covered.
    """
    existing_list = list(existing_tasks)
    kept: list[dict] = []
    dropped: list[dict] = []
    for task in planner_tasks:
        candidate = (task.get("title") or "").strip()
        if not candidate:
            kept.append(task)
            continue
        match = find_duplicate_match(candidate, existing_list, threshold=threshold)
        if match is None:
            kept.append(task)
            continue
        tagged = dict(task)
        tagged["_dedup_matched_id"] = match.get("id")
        tagged["_dedup_matched_title"] = match.get("title")
        dropped.append(tagged)
    return kept, dropped
