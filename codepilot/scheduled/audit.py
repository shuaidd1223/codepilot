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

    _rotate_log_if_needed(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _rotate_log_if_needed(path: Path, max_size_mb: int = 10, keep: int = 2) -> None:
    """Rotate audit log if it exceeds max_size_mb, keeping only last `keep` rotated files."""
    if not path.exists():
        return
    max_bytes = max_size_mb * 1024 * 1024
    if path.stat().st_size <= max_bytes:
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rotated = path.with_name(f"{path.stem}-{today}{path.suffix}")
    if rotated.exists():
        counter = 1
        while rotated.exists():
            rotated = path.with_name(f"{path.stem}-{today}-{counter}{path.suffix}")
            counter += 1
    path.rename(rotated)

    pattern = f"{path.stem}-*{path.suffix}"
    rotated_files = sorted(path.parent.glob(pattern), reverse=True)
    for old in rotated_files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
