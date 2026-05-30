from __future__ import annotations

from typing import Any

from codepilot.core.work_item import build_work_item
from codepilot.core.task_template import missing_task_template_sections
from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import (
    ensure_choice,
    ensure_depends_on,
    ensure_int,
    ensure_str,
    ensure_str_list,
    invalid_arguments,
    task_payload,
)
from codepilot.mcp.tools.tasks import VALID_AGENTS, VALID_PRIORITIES
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
    requester: str | None = None,
    context_links: list[str] | None = None,
    callback: dict[str, Any] | None = None,
    raw_text: str | None = None,
) -> dict[str, Any]:
    project_name = ensure_str(project, "project")
    task_title = ensure_str(title, "title")
    task_content = ensure_str(content, "content", required=False, allow_empty=True) or ""
    task_agent = ensure_choice(agent, "agent", VALID_AGENTS, required=True) or "dual"
    task_priority = ensure_choice(priority, "priority", VALID_PRIORITIES, required=True, upper=True) or "P2"
    task_depends_on = ensure_depends_on(depends_on)
    task_project_path = ensure_str(project_path, "project_path", required=False)
    task_max_retries = ensure_int(max_retries, "max_retries", minimum=0)
    task_requester = ensure_str(requester, "requester", required=False)
    task_context_links = ensure_str_list(context_links, "context_links", required=False)
    if callback is not None and not isinstance(callback, dict):
        raise invalid_arguments("callback must be an object", field="callback")
    task_raw_text = ensure_str(raw_text, "raw_text", required=False)

    # 校验 task-template 必填章节
    if task_content:
        missing = missing_task_template_sections(task_content)
        if missing:
            raise CodePilotToolError(
                f"缺少章节: {', '.join(missing)}",
                code="missing_template_sections",
                details={"missing": missing},
            )

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
        work_item=build_work_item(
            source="mcp",
            requester=task_requester or "mcp",
            context_links=task_context_links,
            callback=callback or {"type": "mcp"},
            raw_text=task_raw_text or task_title,
        ),
    )
    return {"task": task_payload(task)}
