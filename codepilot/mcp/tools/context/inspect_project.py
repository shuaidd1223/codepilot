from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.context import ensure_str_list, resolve_project


DEFAULT_SIGNALS = ["git_log", "failed_tasks", "todos"]


@register_tool(description="Collect inspect signal context for a registered project without creating tasks.")
def inspect_project(project: str, signals: list[str] | None = None) -> dict[str, Any]:
    project_info = resolve_project(project)
    requested_signals = ensure_str_list(signals, "signals") or list(DEFAULT_SIGNALS)

    try:
        from codepilot.commands.inspect import collect_inspection_signal_results

        results = collect_inspection_signal_results(
            str(project_info["name"]),
            Path(str(project_info["path"])),
            signals=tuple(requested_signals),
        )
    except Exception as exc:
        raise CodePilotToolError(str(exc), code="inspect_error", details={"project": project_info["name"]}) from exc

    signals_payload = [
        {
            "key": item.key,
            "title": item.title,
            "order": item.order,
            "enabled": item.enabled,
            "content": item.content,
        }
        for item in results
    ]
    return {
        "project": project_info["name"],
        "project_path": str(Path(str(project_info["path"])).resolve()),
        "signals": signals_payload,
        "count": len(signals_payload),
    }
