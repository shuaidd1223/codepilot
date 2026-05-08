from __future__ import annotations

from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.context import ensure_str, resolve_project


@register_tool(description="Trigger one project-local hook test event through existing hook plumbing.")
def hook_trigger(
    project: str,
    provider: str = "custom",
    event_type: str = "agent.prompt.submitted",
) -> dict[str, Any]:
    project_info = resolve_project(project)
    hook_provider = ensure_str(provider, "provider", required=True)
    hook_event_type = ensure_str(event_type, "event_type", required=True)

    try:
        from codepilot.core import event_plugins, hook_registry

        event = hook_registry.build_hook_test_event(
            str(project_info["name"]),
            hook_event_type or "agent.prompt.submitted",
            provider=hook_provider or "custom",
        )
        log_path = hook_registry.append_hook_log(project_info["path"], event)
        results = event_plugins.dispatch_event_to_sinks(project_info["path"], event)
    except hook_registry.HookRegistryError as exc:
        raise CodePilotToolError(str(exc), code="hook_error", details={"project": project_info["name"]}) from exc

    delivered = len([item for item in results if item.get("status") == "delivered"])
    return {
        "project": project_info["name"],
        "event": event,
        "log_path": str(log_path),
        "logged": True,
        "delivered": delivered,
        "results": results,
    }
