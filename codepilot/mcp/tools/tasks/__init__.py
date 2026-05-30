"""Task-system MCP tools."""

from __future__ import annotations

from codepilot.mcp.tools._helpers import (  # noqa: F401
    ensure_choice,
    ensure_depends_on,
    ensure_int,
    ensure_limit,
    ensure_str,
    invalid_arguments,
    parse_depends_on,
    task_not_found,
    task_payload,
)

VALID_AGENTS = {"dual", "builder", "reviewer", "claude", "codex", "opencode"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
VALID_STATUSES = {"backlog", "in_progress", "done", "failed", "cancelled", "archived"}

from codepilot.mcp.tools.tasks import archive_task as archive_task_module  # noqa: E402,F401
from codepilot.mcp.tools.tasks import create_task as create_task_module  # noqa: E402,F401
from codepilot.mcp.tools.tasks import edit_task as edit_task_module  # noqa: E402,F401
from codepilot.mcp.tools.tasks import generate_breakdown as generate_breakdown_module  # noqa: E402,F401
from codepilot.mcp.tools.tasks import list_tasks as list_tasks_module  # noqa: E402,F401
from codepilot.mcp.tools.tasks import show_task as show_task_module  # noqa: E402,F401
from codepilot.mcp.tools.tasks import stop_task as stop_task_module  # noqa: E402,F401
from codepilot.mcp.tools.tasks import validate_task_template as validate_task_template_module  # noqa: E402,F401

__all__ = [
    "archive_task_module",
    "create_task_module",
    "edit_task_module",
    "generate_breakdown_module",
    "list_tasks_module",
    "show_task_module",
    "stop_task_module",
    "validate_task_template_module",
]
