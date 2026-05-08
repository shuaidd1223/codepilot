"""Audit log helpers for scheduled agent jobs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUDIT_RELATIVE_PATH = Path(".codepilot") / "scheduled" / "audit.jsonl"


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()


def _timestamp(now: str | datetime | None) -> str:
    if isinstance(now, datetime):
        return now.isoformat()
    if now:
        return str(now)
    return datetime.now(timezone.utc).isoformat()


def append_agent_job_audit(
    project_root: str | Path,
    entry: dict[str, Any],
    *,
    now: str | datetime | None = None,
    audit_path: str | Path | None = None,
) -> Path:
    """Append one structured audit record without persisting the prompt body."""

    root = Path(project_root).resolve()
    path = Path(audit_path) if audit_path is not None else root / AUDIT_RELATIVE_PATH
    if not path.is_absolute():
        path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)

    prompt = str(entry.get("prompt") or "")
    record = {
        "timestamp": _timestamp(now),
        "trigger": entry.get("trigger") or {},
        "agent": str(entry.get("agent") or ""),
        "prompt_hash": prompt_hash(prompt),
        "tools": entry.get("tools") or [],
        "cost": float(entry.get("cost") or 0.0),
        "exit_code": entry.get("exit_code"),
    }
    if entry.get("token_usage"):
        record["token_usage"] = entry.get("token_usage") or {}
    if entry.get("agent_chain"):
        record["agent_chain"] = entry.get("agent_chain") or []
    if entry.get("guard"):
        record["guard"] = entry.get("guard") or {}
    if entry.get("notification"):
        record["notification"] = entry.get("notification") or {}
    if "dry_run" in entry:
        record["dry_run"] = bool(entry.get("dry_run"))
    if entry.get("job_name"):
        record["job_name"] = str(entry.get("job_name"))
    if entry.get("project"):
        record["project"] = str(entry.get("project"))

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path
