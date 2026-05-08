"""Project-context MCP tools."""

from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.storage import database as db


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


from codepilot.mcp.tools.context import explore as explore_module  # noqa: E402,F401
from codepilot.mcp.tools.context import hook_trigger as hook_trigger_module  # noqa: E402,F401
from codepilot.mcp.tools.context import inspect_project as inspect_project_module  # noqa: E402,F401
from codepilot.mcp.tools.context import note_add as note_add_module  # noqa: E402,F401
from codepilot.mcp.tools.context import wiki_add as wiki_add_module  # noqa: E402,F401
from codepilot.mcp.tools.context import wiki_query as wiki_query_module  # noqa: E402,F401


__all__ = [
    "explore_module",
    "hook_trigger_module",
    "inspect_project_module",
    "note_add_module",
    "wiki_add_module",
    "wiki_query_module",
]
