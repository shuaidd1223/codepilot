from __future__ import annotations

from typing import Any

from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.tasks import (
    VALID_AGENTS,
    VALID_PRIORITIES,
    ensure_choice,
    ensure_depends_on,
    ensure_int,
    ensure_str,
    task_payload,
)
from codepilot.storage import database as db


@register_tool(description="创建 CodePilot 任务。")
def create_task(
    project: str,
    title: str,
    content: str = "",
    agent: str = "dual",
    priority: str = "P2",
    depends_on: list[int] | None = None,
    project_path: str | None = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    project_name = ensure_str(project, "project")
    task_title = ensure_str(title, "title")
    task_content = ensure_str(content, "content", required=False, allow_empty=True) or ""
    task_agent = ensure_choice(agent, "agent", VALID_AGENTS, required=True) or "dual"
    task_priority = ensure_choice(priority, "priority", VALID_PRIORITIES, required=True, upper=True) or "P2"
    task_depends_on = ensure_depends_on(depends_on)
    task_project_path = ensure_str(project_path, "project_path", required=False)
    task_max_retries = ensure_int(max_retries, "max_retries", minimum=0)

    db.init_db()
    task = db.create_task(
        project=project_name or "",
        title=task_title or "",
        content=task_content,
        agent=task_agent,
        priority=task_priority,
        depends_on=task_depends_on,
        project_path=task_project_path,
        max_retries=task_max_retries,
        source="mcp",
    )
    return {"task": task_payload(task)}
