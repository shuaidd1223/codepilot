from __future__ import annotations

from typing import Any

from codepilot.core.task_template import missing_task_template_sections
from codepilot.mcp.tool_registry import register_tool


@register_tool(description="校验任务 content 是否符合 task-template.md 规范，返回缺少的章节列表。")
def validate_task_template(content: str) -> dict[str, Any]:
    """校验任务内容是否符合模板要求。

    返回 is_valid 和 missing 字段，missing 为空列表表示符合规范。
    空 content 视为有效（create_task 会拒绝无内容的任务）。
    """
    missing = missing_task_template_sections(content) if content else []
    return {
        "is_valid": len(missing) == 0,
        "missing": missing,
    }