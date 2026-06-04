from __future__ import annotations

import pytest

from codepilot.mcp.protocol import (
    CodePilotToolError,
    ProgressEvent,
    normalize_progress_event,
)
from codepilot.mcp.tool_registry import ToolRegistry, register_tool


DEFAULT_MCP_TOOL_NAMES = {
    "archive_task",
    "build_fix",
    "create_task",
    "daemon_status",
    "doctor",
    "edit_task",
    "exec",
    "explore",
    "feishu_notify",
    "feishu_send_to_user",
    "generate_breakdown",
    "hook_trigger",
    "inspect_project",
    "inspect_workflow",
    "list_tasks",
    "note_add",
    "pipeline",
    "run_once",
    "show_task",
    "stop_task",
    "validate_task_template",
    "webhook_invoke",
    "wiki_add",
    "wiki_query",
    "workflow_next",
    "workflow_status",
}


def test_register_tool_builds_schema_from_annotations():
    registry = ToolRegistry()

    @register_tool(registry=registry)
    def add_project_note(project: str, urgent: bool = False) -> dict[str, str]:
        """Add a short note to a project."""
        return {"project": project, "urgent": str(urgent)}

    tool = registry.get("add_project_note")

    assert tool.func is add_project_note
    assert tool.schema["name"] == "add_project_note"
    assert tool.schema["description"] == "Add a short note to a project."
    assert tool.schema["inputSchema"] == {
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "urgent": {"type": "boolean", "default": False},
        },
        "required": ["project"],
        "additionalProperties": False,
    }
    assert tool.schema["outputSchema"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }


def test_default_mcp_registry_loads_26_tools_without_duplicates():
    from codepilot.mcp.tool_registry import default_registry
    from codepilot.mcp.tools import load_default_tools

    load_default_tools()
    names = [tool.name for tool in default_registry.list()]

    assert len(names) == len(set(names))
    assert set(names) == DEFAULT_MCP_TOOL_NAMES


def test_register_tool_rejects_missing_parameter_annotation():
    registry = ToolRegistry()

    with pytest.raises(TypeError, match="project"):

        @register_tool(registry=registry)
        def invalid_tool(project) -> str:
            return str(project)


def test_register_tool_rejects_unsupported_annotation():
    registry = ToolRegistry()

    with pytest.raises(TypeError, match="tags"):

        @register_tool(registry=registry)
        def invalid_tool(tags: set[str]) -> str:
            return ",".join(tags)


def test_codepilot_tool_error_serializes_for_mcp_tool_response():
    error = CodePilotToolError(
        "project not found",
        code="project_missing",
        details={"project": "demo"},
    )

    assert error.to_tool_response() == {
        "isError": True,
        "content": [{"type": "text", "text": "project not found"}],
        "structuredContent": {
            "error": {
                "code": "project_missing",
                "message": "project not found",
                "details": {"project": "demo"},
            }
        },
    }


def test_progress_events_are_normalized_from_yielded_values():
    direct = ProgressEvent(message="starting", progress=0, total=3)
    from_mapping = {"message": "running", "progress": 1, "total": 3, "data": {"phase": "scan"}}
    from_text = "finishing"

    assert normalize_progress_event(direct) is direct
    assert normalize_progress_event(from_mapping).to_dict() == {
        "type": "progress",
        "message": "running",
        "progress": 1,
        "total": 3,
        "data": {"phase": "scan"},
    }
    assert normalize_progress_event(from_text).to_dict() == {
        "type": "progress",
        "message": "finishing",
    }
