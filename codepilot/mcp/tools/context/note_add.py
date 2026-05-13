from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_str, resolve_project


@register_tool(description="向已注册项目的持久记事本追加一条记录。")
def note_add(project: str, content: str, section: str = "working") -> dict[str, Any]:
    project_info = resolve_project(project)
    note_content = ensure_str(content, "content", required=True)
    note_section = ensure_str(section, "section", required=True)

    try:
        from codepilot.commands.note import NoteError, add_note

        result = add_note(project_info, note_content or "", section=note_section or "working")
    except NoteError as exc:
        raise CodePilotToolError(str(exc), code="note_error", details={"project": project_info["name"]}) from exc

    return {"project": project_info["name"], **result}
