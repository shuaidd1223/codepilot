from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.ops import (
    ensure_bool,
    ensure_executor,
    ensure_int,
    ensure_shell,
    resolve_project,
)


@register_tool(description="为已注册项目运行一次有边界的 CodePilot backlog 消费。")
def run_once(
    project: str,
    limit: int = 1,
    dry_run: bool = False,
    shell: str = "auto",
    executor: str = "auto",
    auto_commit: bool = True,
) -> dict[str, Any]:
    project_info = resolve_project(project)
    resolved_limit = ensure_int(limit, "limit", minimum=1)
    resolved_dry_run = ensure_bool(dry_run, "dry_run")
    resolved_shell = ensure_shell(shell)
    resolved_executor = ensure_executor(executor)
    resolved_auto_commit = ensure_bool(auto_commit, "auto_commit")

    try:
        from codepilot.commands.run import run_backlog

        stats = run_backlog(
            str(project_info["name"]),
            once=True,
            limit=resolved_limit,
            dry_run=resolved_dry_run,
            cleanup=True,
            shell=resolved_shell,
            executor=resolved_executor,
            auto_commit=resolved_auto_commit,
        )
    except RuntimeError as exc:
        raise CodePilotToolError(
            str(exc),
            code="run_once_error",
            details={"project": project_info["name"], "limit": resolved_limit},
        ) from exc

    return {"project": project_info["name"], "stats": dict(stats)}
