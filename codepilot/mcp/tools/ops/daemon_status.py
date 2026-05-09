from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.ops import resolve_project


@register_tool(description="Return CodePilot daemon service status for a registered project.")
def daemon_status(project: str) -> dict[str, Any]:
    project_info = resolve_project(project)
    try:
        from codepilot.commands.daemon import daemon_service_status

        status = daemon_service_status(str(project_info["name"]))
    except Exception as exc:
        raise CodePilotToolError(
            str(exc),
            code="daemon_status_error",
            details={"project": project_info["name"]},
        ) from exc

    return {"project": project_info["name"], "status": dict(status)}
