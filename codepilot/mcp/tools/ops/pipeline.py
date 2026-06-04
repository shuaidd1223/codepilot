"""MCP pipeline tool — one-click plan → execute → advance for external agents."""

from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_bool, ensure_int, ensure_str, resolve_project


@register_tool(description="Run the full CodePilot workflow pipeline: plan a requirement, execute its tasks, then advance the workflow. One call replaces plan→run_once→workflow_next.")
def pipeline(
    project: str,
    requirement: str,
    *,
    priority: str = "P2",
    planner: str = "",
    task_agent: str = "",
    executor: str = "builtin",
    max_tasks: int = 0,
    max_retries: int = 0,
    auto_commit: bool = True,
    execute: bool = True,
) -> dict[str, Any]:
    project_info = resolve_project(project)
    title = ensure_str(requirement, "requirement", required=True) or ""
    if not title.strip():
        raise CodePilotToolError(
            "requirement 不能为空",
            code="invalid_requirement",
            details={"project": project},
        )
    resolved_priority = ensure_str(priority, "priority").upper()
    if resolved_priority not in {"P0", "P1", "P2", "P3"}:
        raise CodePilotToolError(
            "priority must be P0/P1/P2/P3",
            code="invalid_priority",
            details={"priority": resolved_priority},
        )
    resolved_max_tasks = ensure_int(max_tasks, "max_tasks", minimum=0)
    resolved_max_retries = ensure_int(max_retries, "max_retries", minimum=0)
    resolved_auto_commit = ensure_bool(auto_commit, "auto_commit")
    resolved_execute = ensure_bool(execute, "execute")

    try:
        from codepilot.commands.auto_workflow import run_requirement_workflow

        result = run_requirement_workflow(
            project_info=project_info,
            title=title.strip(),
            planner=(ensure_str(planner, "planner") or None),
            task_agent=(ensure_str(task_agent, "task_agent") or None),
            priority=resolved_priority,
            max_tasks=resolved_max_tasks,
            max_retries=resolved_max_retries,
            executor=ensure_str(executor, "executor") or "builtin",
            auto_commit=resolved_auto_commit,
            execute=resolved_execute,
            json_mode=False,
            quiet=True,
            task_source="mcp_pipeline",
        )

        return {
            "project": project_info["name"],
            "requirement": title.strip(),
            "summary": result.get("summary", ""),
            "complexity": result.get("complexity", ""),
            "will_execute": result.get("will_execute", False),
            "tasks": [
                {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "priority": t.get("priority"),
                    "status": t.get("status"),
                }
                for t in (result.get("tasks") or [])
            ],
            "run": result.get("run", {}),
        }
    except Exception as exc:
        raise CodePilotToolError(
            str(exc),
            code="pipeline_error",
            details={"project": project_info["name"], "requirement": title.strip()},
        ) from exc
