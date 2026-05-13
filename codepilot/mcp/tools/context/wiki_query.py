from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_int, ensure_str, resolve_project


@register_tool(description="查询已注册项目的本地 Wiki 记忆。")
def wiki_query(project: str, query: str, limit: int = 5) -> dict[str, Any]:
    project_info = resolve_project(project)
    query_text = ensure_str(query, "query", required=True)
    result_limit = ensure_int(limit, "limit", minimum=1, maximum=20)

    try:
        from codepilot.commands.wiki import query_wiki

        results = query_wiki(project_info, query_text or "", limit=result_limit)
    except Exception as exc:
        raise CodePilotToolError(str(exc), code="wiki_error", details={"project": project_info["name"]}) from exc

    return {"project": project_info["name"], "query": query_text, "results": results}
