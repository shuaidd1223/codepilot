from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.ops import (
    ensure_bool,
    ensure_executor,
    ensure_int,
    ensure_str_list,
    resolve_project,
)


@register_tool(description="Run the CodePilot build-fix loop for a failed task.")
def build_fix(
    project: str,
    task_id: int | None = None,
    verify_commands: list[str] | None = None,
    timeout_seconds: int = 300,
    executor: str = "auto",
    auto_commit: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    project_info = resolve_project(project)
    resolved_task_id = None if task_id is None else ensure_int(task_id, "task_id", minimum=1)
    commands = ensure_str_list(verify_commands, "verify_commands") or []
    timeout = ensure_int(timeout_seconds, "timeout_seconds", minimum=1)
    resolved_executor = ensure_executor(executor)
    resolved_auto_commit = ensure_bool(auto_commit, "auto_commit")
    resolved_dry_run = ensure_bool(dry_run, "dry_run")

    try:
        from codepilot.commands.build_fix import BuildFixError, run_build_fix

        return run_build_fix(
            str(project_info["name"]),
            task_id=resolved_task_id,
            verify_commands=tuple(commands),
            timeout_seconds=timeout,
            executor=resolved_executor,
            auto_commit=resolved_auto_commit,
            dry_run=resolved_dry_run,
            json_mode=True,
        )
    except BuildFixError as exc:
        raise CodePilotToolError(
            str(exc),
            code="build_fix_error",
            details={"project": project_info["name"], "task_id": resolved_task_id},
        ) from exc
