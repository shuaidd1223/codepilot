from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.context import ensure_str_list, resolve_project


DEFAULT_SIGNALS = ["git_log", "failed_tasks", "todos"]


def _requested_signal_keys(requested_signals: list[str], signal_specs: tuple[Any, ...]) -> set[str]:
    requested_tokens = {signal.strip().lower() for signal in requested_signals if signal.strip()}
    enabled: set[str] = set()
    for spec in signal_specs:
        if requested_tokens & set(spec.aliases):
            enabled.add(spec.key)
    return enabled


@register_tool(description="为已注册项目收集巡检信号上下文，不创建任务。")
def inspect_project(project: str, signals: list[str] | None = None) -> dict[str, Any]:
    project_info = resolve_project(project)
    requested_signals = ensure_str_list(signals, "signals") or list(DEFAULT_SIGNALS)
    project_name = str(project_info["name"])
    project_path = Path(str(project_info["path"])).resolve()
    if not project_path.is_dir():
        raise CodePilotToolError(
            f"project path not found: {project_path}",
            code="project_path_not_found",
            details={"project": project_name, "project_path": str(project_path)},
        )

    try:
        from codepilot.commands.inspect import INSPECT_SIGNAL_SPECS, collect_inspection_signals

        signal_map = collect_inspection_signals(
            project_name,
            project_path,
            signals=tuple(requested_signals),
        )
    except Exception as exc:
        raise CodePilotToolError(
            str(exc),
            code="inspect_error",
            details={
                "project": project_name,
                "project_path": str(project_path),
                "signals": requested_signals,
            },
        ) from exc

    spec_by_key = {spec.key: spec for spec in INSPECT_SIGNAL_SPECS}
    enabled_keys = _requested_signal_keys(requested_signals, INSPECT_SIGNAL_SPECS)
    signals_payload = [
        {
            "key": key,
            "title": getattr(spec_by_key.get(key), "title", key),
            "order": getattr(spec_by_key.get(key), "order", index),
            "enabled": key in enabled_keys,
            "content": content,
        }
        for index, (key, content) in enumerate(signal_map.items(), start=1)
    ]
    return {
        "project": project_name,
        "project_path": str(project_path),
        "signals": signals_payload,
        "count": len(signals_payload),
        "signal_summary": {
            "requested": requested_signals,
            "returned": len(signals_payload),
            "enabled": [item["key"] for item in signals_payload if item["enabled"]],
            "errors": [],
        },
        "errors": [],
    }
