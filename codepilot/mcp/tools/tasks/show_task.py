from __future__ import annotations

from typing import Any

from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.tasks import ensure_int, task_not_found, task_payload
from codepilot.storage import database as db


@register_tool(description="查看一个 CodePilot 任务及可选任务日志。")
def show_task(task_id: int, include_logs: bool = False) -> dict[str, Any]:
    tid = ensure_int(task_id, "task_id", minimum=1)
    if not isinstance(include_logs, bool):
        from codepilot.mcp.tools.tasks import invalid_arguments

        raise invalid_arguments("include_logs must be a boolean", field="include_logs")

    db.init_db()
    task = db.get_task(tid)
    if not task:
        raise task_not_found(tid)
    logs = [dict(entry) for entry in db.list_task_logs(tid)] if include_logs else []
    return {"task": task_payload(task), "logs": logs}
