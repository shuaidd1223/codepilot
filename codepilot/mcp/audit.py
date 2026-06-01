"""Persistent audit logging for MCP tool operations."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUDIT_RELATIVE_PATH = Path(".codepilot") / "mcp" / "audit.jsonl"
REDACTED = "[redacted]"
_SECRET_MARKERS = ("token", "secret", "password", "authorization", "api_key", "app_id")
_MAX_STRING_LENGTH = 240


def record_mcp_tool_call(
    *,
    project_path: str | Path,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    status: str,
    error: Mapping[str, Any] | None,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """Append one MCP tool audit record under the project workspace."""

    timestamp = (now or _utc_now)()
    record = {
        "timestamp": timestamp.isoformat(),
        "tool": tool_name,
        "arguments_summary": summarize_arguments(arguments or {}),
        "status": status,
        "error": dict(error) if error is not None else None,
    }
    path = Path(project_path).resolve() / AUDIT_RELATIVE_PATH

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_needed(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


def summarize_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _summarize_value(value, key=str(key))
        for key, value in arguments.items()
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _summarize_value(value: Any, *, key: str) -> Any:
    if _is_secret_key(key):
        return REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _summarize_string(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): _summarize_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_summarize_value(item, key=key) for item in value[:20]]
    return repr(value)


def _summarize_string(value: str) -> str:
    if len(value) <= _MAX_STRING_LENGTH:
        return value
    return f"{value[:_MAX_STRING_LENGTH]}..."


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


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
