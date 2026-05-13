from __future__ import annotations

from typing import Any

from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_choice, ensure_limit, ensure_str, task_payload
from codepilot.mcp.tools.tasks import VALID_STATUSES
from codepilot.storage import database as db
from codepilot.webapp.display_sort import sort_tasks_for_display


@register_tool(description="按可选筛选条件列出 CodePilot 任务。")
def list_tasks(
    project: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    project_name = ensure_str(project, "project", required=False)
    task_status = ensure_choice(status, "status", VALID_STATUSES)
    task_limit = ensure_limit(limit)

    db.init_db()
    rows = db.list_tasks(project=project_name, status=task_status)
    tasks = [task_payload(task) for task in sort_tasks_for_display(rows)[:task_limit]]
    return {"tasks": tasks, "count": len(tasks)}
