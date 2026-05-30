from __future__ import annotations

from typing import Any

from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_bool, ensure_str, resolve_project


@register_tool(description="为已注册项目问题收集有限的只读证据。")
def explore(project: str, query: str, use_wiki: bool = True) -> dict[str, Any]:
    project_info = resolve_project(project)
    query_text = ensure_str(query, "query", required=True)
    read_wiki = ensure_bool(use_wiki, "use_wiki")

    from codepilot.commands.explore import explore_project

    return explore_project(
        query_text or "",
        project=str(project_info["name"]),
        cwd=None,
        use_wiki=read_wiki,
    )
