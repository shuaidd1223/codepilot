from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.tasks import VALID_PRIORITIES, ensure_choice, ensure_int, ensure_str
from codepilot.storage import database as db


@register_tool(description="为需求生成结构化任务拆分。")
def generate_breakdown(
    project: str,
    requirement: str,
    planner: str = "codex",
    priority: str = "P2",
    max_tasks: int = 6,
    project_path: str | None = None,
) -> dict[str, Any]:
    project_name = ensure_str(project, "project")
    text = ensure_str(requirement, "requirement")
    task_planner = ensure_str(planner, "planner") or "codex"
    task_priority = ensure_choice(priority, "priority", VALID_PRIORITIES, required=True, upper=True) or "P2"
    task_limit = ensure_int(max_tasks, "max_tasks", minimum=1)
    explicit_path = ensure_str(project_path, "project_path", required=False)

    db.init_db()
    project_record = db.get_project(project_name or "")
    effective_path = explicit_path or (str(project_record.get("path")) if project_record else "")
    if not effective_path:
        raise CodePilotToolError(
            f"project {project_name} is not registered and project_path was not provided",
            code="project_not_found",
            details={"project": project_name},
        )

    try:
        from codepilot.ai_support.service import generate_task_breakdown

        breakdown = generate_task_breakdown(
            text or "",
            project_path=effective_path,
            planner=task_planner,
            max_tasks=task_limit,
            existing_tasks=db.list_tasks(project=project_name),
        )
    except CodePilotToolError:
        raise
    except Exception as exc:
        raise CodePilotToolError(
            f"failed to generate task breakdown: {exc}",
            code="breakdown_generation_failed",
            details={"project": project_name, "planner": task_planner},
        ) from exc

    return {"project": project_name, "priority": task_priority, "breakdown": breakdown}
