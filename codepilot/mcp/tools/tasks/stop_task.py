from __future__ import annotations

from datetime import datetime
from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_int, ensure_str, task_not_found
from codepilot.mcp.tools.tasks._mutation_guard import guard_mcp_task_mutation
from codepilot.mcp.tools.tasks import task_payload
from codepilot.storage import database as db


@register_tool(description="请求停止正在运行的 CodePilot 任务。")
def stop_task(task_id: int, message: str = "") -> dict[str, Any]:
    tid = ensure_int(task_id, "task_id", minimum=1)
    guard_mcp_task_mutation(tid, "stop")
    reason = ensure_str(message, "message", required=False, allow_empty=True) or f"task #{tid} stopped via MCP"

    db.init_db()
    task = db.get_task(tid)
    if not task:
        raise task_not_found(tid)
    if task.get("status") != "in_progress":
        raise CodePilotToolError(
            f"task #{tid} is not in_progress",
            code="invalid_task_status",
            details={"task_id": tid, "status": task.get("status"), "expected": "in_progress"},
        )

    from codepilot.core.runtime import (
        clear_task_runtime,
        is_process_alive,
        request_task_stop,
        stop_process_tree,
        stop_worktree_leftovers,
    )

    request_task_stop(tid, reason)
    pid = task.get("active_pid")
    if pid:
        stop_process_tree(pid)

    refreshed = db.get_task(tid) or task
    if (
        refreshed.get("active_pid")
        and is_process_alive(refreshed.get("active_pid"))
        and refreshed.get("status") == "in_progress"
    ):
        return {"task": task_payload(refreshed), "stop_requested": True}

    clear_task_runtime(
        tid,
        status="cancelled",
        completed_at=refreshed.get("completed_at") or datetime.now().isoformat(),
        error_message=reason,
        stop_requested=0,
        stop_reason=None,
    )
    wt = (refreshed or task).get("worktree_path")
    project_path = (refreshed or task).get("project_path")
    try:
        if wt and wt != project_path:
            stop_worktree_leftovers(wt, wait_seconds=3)
    except Exception:
        pass
    return {"task": task_payload(db.get_task(tid) or refreshed), "stop_requested": False}
