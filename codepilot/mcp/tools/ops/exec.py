from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.ops import (
    ALLOWED_EXEC_COMMANDS,
    ensure_str,
    ensure_str_list,
    invoke_cli_json,
    project_cli_args,
    resolve_project,
)


def _exec_cli_args(project: str, command: str, args: list[str] | None) -> list[str]:
    spec = ALLOWED_EXEC_COMMANDS.get(command)
    if spec is None:
        raise CodePilotToolError(
            f"command is not allowed for MCP ops exec: {command}",
            code="command_not_allowed",
            details={"command": command, "allowed": sorted(ALLOWED_EXEC_COMMANDS)},
        )
    if args and not spec.allow_user_args:
        raise CodePilotToolError(
            f"command does not allow extra arguments: {command}",
            code="command_args_not_allowed",
            details={"command": command},
        )

    cli_args = list(spec.cli_args)
    if command in {"doctor", "status", "hook.validate", "skill.list"}:
        cli_args.extend(project_cli_args(project))
    cli_args.append("--json")
    if args:
        cli_args.extend(args)
    return cli_args


@register_tool(name="exec", description="通过当前 click 入口执行白名单内的 CodePilot CLI 操作。")
def exec_tool(project: str, command: str, args: list[str] | None = None) -> dict[str, Any]:
    project_info = resolve_project(project)
    command_key = ensure_str(command, "command", required=True) or ""
    command_args = ensure_str_list(args, "args") or []
    cli_args = _exec_cli_args(str(project_info["name"]), command_key, command_args)
    return invoke_cli_json(cli_args)
