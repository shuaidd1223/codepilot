from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.external import ensure_int, ensure_str, resolve_project


@register_tool(description="Invoke the configured project webhook through CodePilot's existing webhook API.")
def webhook_invoke(
    project: str,
    task_id: int,
    task_title: str,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    status: str = "",
    summary: str = "",
) -> dict[str, Any]:
    project_info = resolve_project(project)
    tid = ensure_int(task_id, "task_id", minimum=1)
    title = ensure_str(task_title, "task_title", required=True) or ""
    event_name = ensure_str(event, "event", required=True) or ""
    event_phase = ensure_str(phase, "phase", required=False, allow_empty=True) or ""
    event_level = ensure_str(level, "level", required=True) or "info"
    event_message = ensure_str(message, "message", required=False, allow_empty=True) or ""
    event_status = ensure_str(status, "status", required=False, allow_empty=True) or ""
    event_summary = ensure_str(summary, "summary", required=False, allow_empty=True) or ""

    from codepilot.webapp import webhook as webhook_mod

    config = webhook_mod._get_webhook_config(str(project_info["path"]))
    if not config.get("enabled") or not config.get("webhook_url"):
        raise CodePilotToolError(
            f"webhook is not configured for project: {project_info['name']}",
            code="webhook_not_configured",
            details={"project": project_info["name"], "project_path": str(project_info["path"])},
        )

    sent = webhook_mod.notify_task_event(
        str(project_info["path"]),
        tid,
        title,
        event=event_name,
        phase=event_phase,
        level=event_level,
        message=event_message,
        status=event_status,
        summary=event_summary,
    )
    if not sent:
        raise CodePilotToolError(
            f"webhook delivery failed for project: {project_info['name']}",
            code="webhook_invoke_failed",
            details={"project": project_info["name"], "task_id": tid},
        )
    return {
        "ok": True,
        "sent": True,
        "project": project_info["name"],
        "task_id": tid,
    }
