"""Shared helpers for MCP tool modules."""

from __future__ import annotations

import json
from typing import Any

from codepilot.core.work_item import task_work_item_payload
from codepilot.mcp.protocol import CodePilotToolError
from codepilot.storage import database as db


# ── Argument validation ──────────────────────────────────────────────────────


def invalid_arguments(message: str, **details: Any) -> CodePilotToolError:
    return CodePilotToolError(message, code="invalid_arguments", details=details)


def ensure_str(
    value: Any,
    field: str,
    *,
    required: bool = True,
    allow_empty: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise invalid_arguments(f"{field} is required", field=field)
        return None
    if not isinstance(value, str):
        raise invalid_arguments(f"{field} must be a string", field=field)
    text = value.strip()
    if required and not text:
        raise invalid_arguments(f"{field} cannot be empty", field=field)
    if not allow_empty and value != "" and not text:
        raise invalid_arguments(f"{field} cannot be blank", field=field)
    return text if not allow_empty else value


def ensure_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_arguments(f"{field} must be an integer", field=field)
    if minimum is not None and value < minimum:
        raise invalid_arguments(
            f"{field} must be greater than or equal to {minimum}",
            field=field,
            minimum=minimum,
        )
    if maximum is not None and value > maximum:
        raise invalid_arguments(
            f"{field} must be less than or equal to {maximum}",
            field=field,
            maximum=maximum,
        )
    return value


def ensure_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise invalid_arguments(f"{field} must be a boolean", field=field)
    return value


def ensure_str_list(
    value: Any,
    field: str,
    *,
    required: bool = False,
    allow_empty: bool = True,
) -> list[str] | None:
    if value is None:
        if required:
            raise invalid_arguments(f"{field} is required", field=field)
        return None
    if not isinstance(value, list):
        raise invalid_arguments(f"{field} must be a list of strings", field=field)
    result: list[str] = []
    for item in value:
        text = ensure_str(item, field, required=True)
        if text is not None:
            result.append(text)
    if not result and not allow_empty:
        raise invalid_arguments(f"{field} cannot be empty", field=field)
    return result


def ensure_choice(
    value: Any,
    field: str,
    choices: set[str],
    *,
    required: bool = False,
    upper: bool = False,
) -> str | None:
    text = ensure_str(value, field, required=required)
    if text is None:
        return None
    normalized = text.upper() if upper else text.lower()
    if normalized not in choices:
        raise CodePilotToolError(
            f"{field} has invalid value: {value}",
            code=f"invalid_task_{field}",
            details={"field": field, "value": value, "allowed": sorted(choices)},
        )
    return normalized


# ── Project resolution ───────────────────────────────────────────────────────


def resolve_project(project: Any) -> dict[str, Any]:
    project_name = ensure_str(project, "project", required=True)
    db.init_db()
    found = db.get_project(project_name or "")
    if not found:
        raise CodePilotToolError(
            f"project not found: {project_name}",
            code="project_not_found",
            details={"project": project_name},
        )
    return dict(found)


# ── Task-specific helpers ────────────────────────────────────────────────────


def ensure_depends_on(value: Any, field: str = "depends_on") -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise invalid_arguments(f"{field} must be a list of task ids", field=field)
    result: list[int] = []
    for item in value:
        result.append(ensure_int(item, field, minimum=1))
    return result or None


def ensure_limit(value: Any, field: str = "limit") -> int:
    return ensure_int(value, field, minimum=1)


def task_not_found(task_id: int) -> CodePilotToolError:
    return CodePilotToolError(
        f"task #{task_id} not found",
        code="task_not_found",
        details={"task_id": task_id},
    )


def parse_depends_on(raw: Any) -> list[int]:
    if raw in (None, ""):
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    ids: list[int] = []
    for item in parsed:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def task_payload(task: dict[str, Any]) -> dict[str, Any]:
    payload = dict(task)
    payload["depends_on_ids"] = parse_depends_on(task.get("depends_on"))
    payload["work_item"] = task_work_item_payload(task)
    return payload
