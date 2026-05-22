from __future__ import annotations

from typing import Any

import click

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import resolve_project


def _workflow_error(exc: Exception, *, project: str) -> CodePilotToolError:
    return CodePilotToolError(
        str(exc),
        code="workflow_error",
        details={"project": project},
    )


@register_tool(description="运行只读 inspect dry-run，并写入项目 workflow context。")
def inspect_workflow(project: str, max_new: int | None = None, planner: str | None = None) -> dict[str, Any]:
    project_info = resolve_project(project)
    try:
        from codepilot.commands.inspect_workflow import run_inspect_preview_to_workflow

        return run_inspect_preview_to_workflow(project_info, max_new=max_new, planner=planner)
    except Exception as exc:
        raise _workflow_error(exc, project=project) from exc


@register_tool(description="读取项目 workflow 状态和 Agent Session。")
def workflow_status(project: str, mode: str | None = None) -> dict[str, Any]:
    resolve_project(project)
    try:
        from codepilot.commands.workflow import workflow_status_payload

        return workflow_status_payload(project, mode=mode)
    except Exception as exc:
        raise _workflow_error(exc, project=project) from exc


@register_tool(description="列出或执行项目 workflow next_actions；只执行 CodePilot allowlist 动作。")
def workflow_next(
    project: str,
    action_id: str | None = None,
    mode: str | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    resolve_project(project)
    try:
        from codepilot.commands.workflow import execute_workflow_next_action, workflow_next_payload

        if action_id:
            return execute_workflow_next_action(
                project,
                action_id,
                mode=mode,
                allow_high_risk=allow_high_risk,
            )
        return workflow_next_payload(project, mode=mode)
    except click.ClickException as exc:
        raise _workflow_error(exc, project=project) from exc
    except Exception as exc:
        raise _workflow_error(exc, project=project) from exc

