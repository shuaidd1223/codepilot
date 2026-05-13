from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_str, ensure_str_list, resolve_project


@register_tool(description="向已注册项目的本地 Wiki 记忆添加页面。")
def wiki_add(
    project: str,
    title: str,
    body: str,
    slug: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    project_info = resolve_project(project)
    page_title = ensure_str(title, "title", required=True)
    page_body = ensure_str(body, "body", required=True)
    page_slug = ensure_str(slug, "slug", required=False) if slug is not None else None
    page_tags = ensure_str_list(tags, "tags") or []

    try:
        from codepilot.commands.wiki import WikiError, add_wiki_note

        page = add_wiki_note(
            project_info,
            title=page_title or "",
            body=page_body or "",
            slug=page_slug,
            tags=page_tags,
            source="mcp.wiki_add",
        )
    except WikiError as exc:
        raise CodePilotToolError(str(exc), code="wiki_error", details={"project": project_info["name"]}) from exc

    return {"project": project_info["name"], "page": page}
