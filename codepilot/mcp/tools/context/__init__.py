"""Project-context MCP tools."""

from __future__ import annotations

from codepilot.mcp.tools._helpers import (  # noqa: F401
    ensure_bool,
    ensure_int,
    ensure_str,
    ensure_str_list,
    invalid_arguments,
    resolve_project,
)

from codepilot.mcp.tools.context import explore as explore_module  # noqa: E402,F401
from codepilot.mcp.tools.context import hook_trigger as hook_trigger_module  # noqa: E402,F401
from codepilot.mcp.tools.context import inspect_project as inspect_project_module  # noqa: E402,F401
from codepilot.mcp.tools.context import note_add as note_add_module  # noqa: E402,F401
from codepilot.mcp.tools.context import workflow as workflow_module  # noqa: E402,F401
from codepilot.mcp.tools.context import wiki_add as wiki_add_module  # noqa: E402,F401
from codepilot.mcp.tools.context import wiki_query as wiki_query_module  # noqa: E402,F401

__all__ = [
    "explore_module",
    "hook_trigger_module",
    "inspect_project_module",
    "note_add_module",
    "workflow_module",
    "wiki_add_module",
    "wiki_query_module",
]
