from __future__ import annotations

from typing import Any

from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.ops import ensure_bool, invoke_cli_json, project_cli_args, resolve_project


@register_tool(description="Run CodePilot doctor checks for a registered project.")
def doctor(project: str, services: bool = False, fix: bool = False) -> dict[str, Any]:
    project_info = resolve_project(project)
    include_services = ensure_bool(services, "services")
    fix_mode = ensure_bool(fix, "fix")
    args = ["doctor", *project_cli_args(str(project_info["name"])), "--json"]
    if include_services:
        args.append("--services")
    if fix_mode:
        args.append("--fix")
    return invoke_cli_json(args)
