# CodePilot
# Author: 帅呆呆 <2264505396@qq.com>
# Repository: https://gitee.com/shuai_dd/workflow
# License: MIT
"""Built-in MCP tool packages."""

from __future__ import annotations

import importlib

from codepilot.mcp.tool_registry import ToolDefinition, default_registry


DEFAULT_TOOL_PACKAGES = (
    "codepilot.mcp.tools.context",
    "codepilot.mcp.tools.tasks",
    "codepilot.mcp.tools.external",
    "codepilot.mcp.tools.ops",
)


def load_default_tools() -> list[ToolDefinition]:
    """Import all built-in tool packages so their decorators populate the registry."""
    for package in DEFAULT_TOOL_PACKAGES:
        importlib.import_module(package)
    return default_registry.list()


__all__ = ["DEFAULT_TOOL_PACKAGES", "load_default_tools"]
