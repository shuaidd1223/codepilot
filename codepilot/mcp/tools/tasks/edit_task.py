from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import (
    ensure_choice,
    ensure_depends_on,
    ensure_int,
    ensure_str,
    invalid_arguments,
    task_not_found,
    task_payload,
)
from codepilot.mcp.tools.tasks import VALID_AGENTS, VALID_PRIORITIES, VALID_STATUSES
from codepilot.storage import database as db


@register_tool(description="编辑指定 CodePilot 任务字段。")
def edit_task(
    task_id: int,
    title: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    agent: str | None = None,
    depends_on: list[int] | None = None,
) -> dict[str, Any]:
    tid = ensure_int(task_id, "task_id", minimum=1)

    updates: dict[str, Any] = {}
    task_title = ensure_str(title, "title", required=False)
    if task_title is not None:
        updates["title"] = task_title
    task_priority = ensure_choice(priority, "priority", VALID_PRIORITIES, upper=True)
    if task_priority is not None:
        updates["priority"] = task_priority
    task_status = _ensure_status(status)
    if task_status is not None:
        updates["status"] = task_status
        if task_status != "in_progress":
            updates.update(
                {
                    "run_phase": None,
                    "heartbeat_at": None,
                    "active_pid": None,
                    "current_log_path": None,
                    "last_output": None,
                    "stop_requested": 0,
                    "stop_reason": None,
                }
            )
    task_agent = ensure_choice(agent, "agent", VALID_AGENTS)
    if task_agent is not None:
        updates["agent"] = task_agent
    if depends_on is not None:
        updates["depends_on"] = ensure_depends_on(depends_on)

    if not updates:
        raise invalid_arguments("no task fields were provided")

    db.init_db()
    if not db.get_task(tid):
        raise task_not_found(tid)
    updated = db.update_task(tid, **updates)
    if not updated:
        raise task_not_found(tid)
    return {"task": task_payload(updated), "updated_fields": sorted(updates)}


def _ensure_status(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid_arguments("status must be a string", field="status")
    normalized = value.strip().lower()
    if normalized not in VALID_STATUSES:
        raise CodePilotToolError(
            f"status has invalid value: {value}",
            code="invalid_task_status",
            details={"field": "status", "value": value, "allowed": sorted(VALID_STATUSES)},
        )
    return normalized
