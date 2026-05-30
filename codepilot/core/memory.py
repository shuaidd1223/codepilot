"""Project-local memory event log.

Phase 1 memory is an append-only factual log. It records observations that can
later be summarized into wiki/note candidates, but it does not mutate durable
knowledge by itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMORY_EVENTS_PATH = Path(".codepilot") / "memory" / "events.jsonl"
MEMORY_CANDIDATES_PATH = Path(".codepilot") / "memory" / "candidates.jsonl"
MEMORY_AUTOCAPTURE_PATH = Path(".codepilot") / "memory" / "autocapture.md"
_SECRET_RE = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|app_secret|private[_-]?key|FEISHU_APP_SECRET)\s*[:=]",
    re.IGNORECASE,
)
_VOLATILE_SEED_KEYS = {"context_path", "source_path", "project_path", "timestamp", "updated_at", "created_at", "event_id"}
_DEFAULT_SCORE = 50


class MemoryError(ValueError):
    """Raised when a memory operation cannot be accepted safely."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_text(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _project_path(project_info: dict[str, Any]) -> Path:
    path = Path(str(project_info.get("path") or "")).expanduser().resolve()
    if not path.is_dir():
        raise MemoryError(f"项目路径不存在：{path}")
    return path


def memory_events_path(project_info: dict[str, Any]) -> Path:
    return _project_path(project_info) / MEMORY_EVENTS_PATH


def memory_candidates_path(project_info: dict[str, Any]) -> Path:
    return _project_path(project_info) / MEMORY_CANDIDATES_PATH


def memory_autocapture_path(project_info: dict[str, Any]) -> Path:
    return _project_path(project_info) / MEMORY_AUTOCAPTURE_PATH


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _reject_secret_like(value: Any) -> None:
    if _SECRET_RE.search(_json_text(value)):
        raise MemoryError("memory event 不接受疑似 secret、token、password、api key 或 app_secret 内容。")


def _event_id(seed: dict[str, Any]) -> str:
    digest = hashlib.sha256(_json_text(seed).encode("utf-8")).hexdigest()[:16]
    return f"mem_{digest}"


def _candidate_id(seed: dict[str, Any]) -> str:
    digest = hashlib.sha256(_json_text(seed).encode("utf-8")).hexdigest()[:16]
    return f"memcand_{digest}"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600), "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _write_jsonl(path: Path, payloads: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8", newline="\n") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _candidate_seed(event: dict[str, Any]) -> dict[str, Any]:
    stable_details = _stable_seed_value(dict(event.get("details") or {}))
    return {
        "event_type": event.get("event_type"),
        "source": event.get("source"),
        "summary": event.get("summary"),
        "details": stable_details,
    }


def _stable_seed_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_seed_value(raw_value)
            for key, raw_value in sorted(value.items())
            if str(key) not in _VOLATILE_SEED_KEYS and not str(key).endswith("_path")
        }
    if isinstance(value, list):
        return [_stable_seed_value(item) for item in value]
    return value


def _candidate_feedback(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "")
    details = dict(event.get("details") or {})
    action_id = str(details.get("action_id") or "")
    status = str(details.get("status") or "")

    if event_type == "workflow.action_executed":
        if action_id.startswith("promote_inspect_report_"):
            return {
                "feedback": "positive",
                "score": 90,
                "signals": ["inspect_report_promoted", "workflow_action_executed"],
                "score_reason": "inspect report-only candidate was promoted",
            }
        if action_id == "create_inspect_tasks":
            return {
                "feedback": "positive",
                "score": 82,
                "signals": ["inspect_tasks_created", "workflow_action_executed"],
                "score_reason": "inspect candidates were converted into tasks",
            }
        if action_id == "plan_from_inspect":
            return {
                "feedback": "positive",
                "score": 74,
                "signals": ["inspect_plan_requested", "workflow_action_executed"],
                "score_reason": "inspect context was used to request a plan",
            }
        if action_id.startswith(("ignore_inspect_report_", "delete_inspect_report_", "archive_inspect_report_")):
            return {
                "feedback": "negative",
                "score": 20,
                "signals": ["inspect_report_dismissed", "workflow_action_executed"],
                "score_reason": "inspect report-only candidate was dismissed",
            }
        return {
            "feedback": "positive",
            "score": 68,
            "signals": ["workflow_action_executed"],
            "score_reason": "workflow action was executed",
        }

    if event_type == "task.updated":
        if status == "done":
            return {
                "feedback": "positive",
                "score": 88,
                "signals": ["task_done", "task_terminal_status"],
                "score_reason": "task reached done status",
            }
        if status == "failed":
            return {
                "feedback": "negative",
                "score": 25,
                "signals": ["task_failed", "task_terminal_status"],
                "score_reason": "task reached failed status",
            }
        if status == "cancelled":
            return {
                "feedback": "negative",
                "score": 35,
                "signals": ["task_cancelled", "task_terminal_status"],
                "score_reason": "task reached cancelled status",
            }

    if event_type == "task.archived":
        return {
            "feedback": "positive",
            "score": 84,
            "signals": ["task_archived", "task_manual_feedback"],
            "score_reason": "completed task was archived",
        }

    if event_type == "task.retried":
        return {
            "feedback": "positive",
            "score": 65,
            "signals": ["task_retried", "task_manual_feedback"],
            "score_reason": "task was retried instead of discarded",
        }

    if event_type == "task.deleted":
        return {
            "feedback": "negative",
            "score": 15,
            "signals": ["task_deleted", "task_manual_feedback"],
            "score_reason": "task was deleted",
        }

    if event_type == "inspect.report_feedback":
        status = str(details.get("status") or "")
        if status in {"ignored", "deleted", "archived"}:
            return {
                "feedback": "negative",
                "score": 18 if status != "archived" else 28,
                "signals": [f"inspect_report_{status}", "inspect_report_feedback"],
                "score_reason": f"inspect report-only candidate was {status}",
            }

    if event_type == "inspect.workflow_context_written":
        return {
            "feedback": "neutral",
            "score": 55,
            "signals": ["inspect_context_written"],
            "score_reason": "inspect context was written for later workflow use",
        }

    return {
        "feedback": "neutral",
        "score": _DEFAULT_SCORE,
        "signals": ["memory_event"],
        "score_reason": "low-risk factual memory event",
    }


def _candidate_from_event(event: dict[str, Any]) -> dict[str, Any]:
    candidate_id = _candidate_id(_candidate_seed(event))
    timestamp = event.get("timestamp") or _now_iso()
    feedback = _candidate_feedback(event)
    return {
        "candidate_id": candidate_id,
        "source_event_id": event.get("event_id"),
        "source_event_ids": [event.get("event_id")] if event.get("event_id") else [],
        "created_at": timestamp,
        "first_seen_at": timestamp,
        "last_seen_at": timestamp,
        "seen_count": 1,
        "event_type": event.get("event_type"),
        "source": event.get("source"),
        "summary": event.get("summary"),
        "details": dict(event.get("details") or {}),
        "tags": list(event.get("tags") or []),
        "confidence": event.get("confidence") or "fact",
        "feedback": feedback["feedback"],
        "score": feedback["score"],
        "signals": feedback["signals"],
        "score_reason": feedback["score_reason"],
        "status": "auto_promoted",
        "target": "memory.autocapture",
        "reason": "low_risk_fact_event",
    }


def _as_score(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return _DEFAULT_SCORE


def _merge_candidate(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    old_seen = int(existing.get("seen_count") or 1)
    merged["seen_count"] = old_seen + 1
    merged["last_seen_at"] = incoming.get("last_seen_at") or incoming.get("created_at") or _now_iso()
    merged.setdefault("first_seen_at", existing.get("created_at") or incoming.get("created_at") or merged["last_seen_at"])
    merged.setdefault("source_event_id", existing.get("source_event_id") or incoming.get("source_event_id"))

    source_ids = list(existing.get("source_event_ids") or [])
    if not source_ids and existing.get("source_event_id"):
        source_ids.append(existing["source_event_id"])
    for event_id in incoming.get("source_event_ids") or []:
        if event_id and event_id not in source_ids:
            source_ids.append(event_id)
    merged["source_event_ids"] = source_ids

    current_score = _as_score(existing.get("score"))
    incoming_score = _as_score(incoming.get("score"))
    incoming_feedback = str(incoming.get("feedback") or "neutral")
    if incoming_feedback == "positive":
        merged["score"] = min(95, max(current_score, incoming_score) + 2)
        merged["feedback"] = "positive"
    elif incoming_feedback == "negative":
        merged["score"] = max(0, min(current_score, incoming_score) - 2)
        merged["feedback"] = "negative"
    else:
        merged["score"] = round((current_score + incoming_score) / 2)
        merged["feedback"] = str(existing.get("feedback") or incoming_feedback or "neutral")

    signals = sorted({str(signal) for signal in (existing.get("signals") or []) + (incoming.get("signals") or []) if signal})
    merged["signals"] = signals
    merged["score_reason"] = incoming.get("score_reason") or existing.get("score_reason") or "low-risk factual memory event"
    return merged


def _render_autocapture(project_info: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    lines = [
        "# CodePilot Auto Memory",
        "",
        "自动从 workflow 事实事件沉淀的项目记忆摘要。该文件由 CodePilot 维护，不用于存放 secret。",
        "",
    ]
    if not candidates:
        lines.append("-")
        return "\n".join(lines).rstrip() + "\n"
    for item in sorted(candidates, key=lambda candidate: str(candidate.get("created_at") or ""), reverse=True):
        timestamp = str(item.get("created_at") or "")[:19]
        candidate_id = str(item.get("candidate_id") or "")
        event_type = str(item.get("event_type") or "")
        summary = _compact_text(item.get("summary") or "", limit=240)
        score = _as_score(item.get("score"))
        feedback = str(item.get("feedback") or "neutral")
        seen_count = int(item.get("seen_count") or 1)
        lines.append(f"- {timestamp} | `{candidate_id}` | `{event_type}` | score={score} | feedback={feedback} | seen={seen_count} | {summary}")
    return "\n".join(lines).rstrip() + "\n"


def _write_autocapture(project_info: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    path = memory_autocapture_path(project_info)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_autocapture(project_info, candidates), encoding="utf-8", newline="\n")


def _auto_capture_candidate(project_info: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    candidate = _candidate_from_event(event)
    candidates = read_memory_candidates(project_info, limit=0)
    _reject_secret_like(candidate)
    for index, item in enumerate(candidates):
        if item.get("candidate_id") != candidate["candidate_id"]:
            continue
        merged = _merge_candidate(item, candidate)
        _reject_secret_like(merged)
        candidates[index] = merged
        _write_jsonl(memory_candidates_path(project_info), candidates)
        _write_autocapture(project_info, candidates)
        return merged
    path = memory_candidates_path(project_info)
    _append_jsonl(path, candidate)
    candidates.append(candidate)
    _write_autocapture(project_info, candidates)
    return candidate


def append_memory_event(
    project_info: dict[str, Any],
    *,
    event_type: str,
    source: str,
    summary: str,
    details: dict[str, Any] | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    confidence: str = "fact",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Append one factual memory event to project-local JSONL."""
    event_type = _compact_text(event_type, limit=120)
    source = _compact_text(source, limit=120)
    summary = _compact_text(summary, limit=500)
    if not event_type:
        raise MemoryError("memory event_type 不能为空。")
    if not source:
        raise MemoryError("memory source 不能为空。")
    if not summary:
        raise MemoryError("memory summary 不能为空。")
    details = dict(details or {})
    clean_tags = sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})
    payload: dict[str, Any] = {
        "timestamp": timestamp or _now_iso(),
        "project": str(project_info.get("name") or ""),
        "event_type": event_type,
        "source": source,
        "summary": summary,
        "details": details,
        "tags": clean_tags,
        "confidence": _compact_text(confidence or "fact", limit=40),
    }
    _reject_secret_like(payload)
    payload["event_id"] = _event_id(payload)
    path = memory_events_path(project_info)
    _append_jsonl(path, payload)
    _auto_capture_candidate(project_info, payload)
    return payload


def read_memory_events(
    project_info: dict[str, Any],
    *,
    limit: int = 0,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Read project memory events in newest-first order."""
    path = memory_events_path(project_info)
    wanted = str(event_type or "").strip()
    events: list[dict[str, Any]] = []
    for payload in _read_jsonl(path):
        if wanted and str(payload.get("event_type") or "") != wanted:
            continue
        events.append(payload)
    events.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    if limit > 0:
        return events[:limit]
    return events


def read_memory_candidates(project_info: dict[str, Any], *, limit: int = 0) -> list[dict[str, Any]]:
    """Read auto-captured memory candidates in newest-first order."""
    candidates = _read_jsonl(memory_candidates_path(project_info))
    candidates.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    if limit > 0:
        return candidates[:limit]
    return candidates
