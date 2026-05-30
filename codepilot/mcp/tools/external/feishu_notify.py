from __future__ import annotations

from typing import Any

from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_int, ensure_str, ensure_str_list, resolve_project
from codepilot.mcp.tools.external import (
    PerMinuteRateLimiter,
    feishu_rate_limit_error_response,
)


FEISHU_NOTIFY_LIMITER = PerMinuteRateLimiter(limit_per_minute=20)


@register_tool(description="通过现有飞书机器人 API 发送 CodePilot 任务事件通知。")
def feishu_notify(
    project: str,
    task_id: int,
    task_title: str,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    status: str = "",
    summary: str = "",
    chat_ids: list[str] | None = None,
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
    targets = ensure_str_list(chat_ids, "chat_ids", required=False, allow_empty=False)

    decision = FEISHU_NOTIFY_LIMITER.check("feishu_notify")
    if not decision.allowed:
        return feishu_rate_limit_error_response(decision)

    from codepilot import feishu_bot

    sent = feishu_bot.notify_feishu_task_event(
        project_name=str(project_info["name"]),
        project_path=str(project_info["path"]),
        task_id=tid,
        task_title=title,
        event=event_name,
        phase=event_phase,
        level=event_level,
        message=event_message,
        status=event_status,
        summary=event_summary,
        chat_ids=targets,
    )
    return {
        "ok": bool(sent),
        "sent": bool(sent),
        "project": project_info["name"],
        "task_id": tid,
    }
