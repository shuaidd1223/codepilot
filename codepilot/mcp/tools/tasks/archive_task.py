from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_int, task_not_found
from codepilot.mcp.tools.tasks._mutation_guard import guard_mcp_task_mutation
from codepilot.mcp.tools.tasks import task_payload
from codepilot.storage import database as db


@register_tool(description="归档已完成的 CodePilot 任务。")
def archive_task(task_id: int) -> dict[str, Any]:
    tid = ensure_int(task_id, "task_id", minimum=1)
    guard_mcp_task_mutation(tid, "archive")

    db.init_db()
    task = db.get_task(tid)
    if not task:
        raise task_not_found(tid)
    if task.get("status") == "archived":
        return {"task": task_payload(task), "archived": False}
    if task.get("status") != "done":
        raise CodePilotToolError(
            f"task #{tid} must be done before archive",
            code="invalid_task_status",
            details={"task_id": tid, "status": task.get("status"), "expected": "done"},
        )
    updated = db.update_task(tid, status="archived")
    if not updated:
        raise task_not_found(tid)
    return {"task": task_payload(updated), "archived": True}
