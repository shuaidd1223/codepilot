"""MCP integration primitives for CodePilot tools."""

from codepilot.mcp.protocol import CodePilotToolError, ProgressEvent, normalize_progress_event
from codepilot.mcp.tool_registry import ToolDefinition, ToolRegistry, register_tool

__all__ = [
    "CodePilotToolError",
    "ProgressEvent",
    "ToolDefinition",
    "ToolRegistry",
    "normalize_progress_event",
    "register_tool",
]
