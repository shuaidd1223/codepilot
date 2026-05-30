"""Shared intake work-item metadata helpers.

The task table already has a compact ``source`` column. Richer intake metadata
is persisted in the existing per-task execution artifact under
``metadata.work_item`` so old rows keep loading without a schema migration.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codepilot.core.workflow_state import read_task_execution_artifacts, write_task_execution_artifacts


MAX_RAW_TEXT_CHARS = 4000
MAX_RAW_TEXT_SUMMARY_CHARS = 240
_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|access[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def raw_text_summary(value: Any) -> str:
    return _compact_text(value, limit=MAX_RAW_TEXT_SUMMARY_CHARS)


def _safe_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return _compact_text(value, limit=200)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _compact_text(value, limit=1000)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = _compact_text(key, limit=80)
            if not safe_key:
                continue
            if _SENSITIVE_KEY_RE.search(safe_key):
                safe[safe_key] = "[redacted]"
            else:
                safe[safe_key] = _safe_json_value(item, depth=depth + 1)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_safe_json_value(item, depth=depth + 1) for item in list(value)[:20]]
    return _compact_text(value, limit=1000)


def _context_links(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, (list, tuple, set)) else [value]
    links: list[Any] = []
    for item in list(raw_items)[:20]:
        if item in (None, ""):
            continue
        safe = _safe_json_value(item)
        if safe in (None, ""):
            continue
        links.append(safe)
    return links


def _callback(value: Any) -> dict[str, Any]:
    safe = _safe_json_value(value)
    return safe if isinstance(safe, dict) else {}


def build_work_item(
    *,
    source: Any = "user",
    requester: Any = "",
    context_links: Any = None,
    callback: Any = None,
    raw_text: Any = "",
) -> dict[str, Any]:
    raw = _compact_text(raw_text, limit=MAX_RAW_TEXT_CHARS)
    return {
        "source": _compact_text(source, limit=80) or "user",
        "requester": _compact_text(requester, limit=160),
        "context_links": _context_links(context_links),
        "callback": _callback(callback),
        "raw_text": raw,
        "raw_text_summary": raw_text_summary(raw),
    }


def coerce_work_item(
    value: Any,
    *,
    fallback_source: Any = "user",
    fallback_raw_text: Any = "",
) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return build_work_item(
        source=raw.get("source") or fallback_source or "user",
        requester=raw.get("requester") or "",
        context_links=raw.get("context_links"),
        callback=raw.get("callback"),
        raw_text=raw.get("raw_text") or fallback_raw_text or "",
    )


def persist_task_work_item(task: dict[str, Any], work_item: Any) -> dict[str, Any] | None:
    if work_item is None:
        return None
    task_id = task.get("id")
    project_path = str(task.get("project_path") or "").strip()
    if not task_id or not project_path:
        return None
    normalized = coerce_work_item(
        work_item,
        fallback_source=task.get("source") or "user",
        fallback_raw_text=task.get("title") or "",
    )
    write_task_execution_artifacts(
        project_path,
        int(task_id),
        source=normalized["source"],
        metadata={"work_item": normalized},
    )
    return normalized


def task_work_item_payload(task: dict[str, Any]) -> dict[str, Any]:
    fallback = build_work_item(source=task.get("source") or "user")
    task_id = task.get("id")
    project_path = str(task.get("project_path") or "").strip()
    if not task_id or not project_path:
        return fallback
    try:
        artifact = read_task_execution_artifacts(Path(project_path), int(task_id))
    except Exception:
        artifact = None
    metadata = artifact.get("metadata") if isinstance(artifact, dict) else None
    raw_work_item = metadata.get("work_item") if isinstance(metadata, dict) else None
    return coerce_work_item(
        raw_work_item,
        fallback_source=task.get("source") or "user",
    )
