"""OpenCode MCP launch-plan builder."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from codepilot.mcp.launchers import (
    LaunchPlan,
    MCPServerSpec,
    json_config_text,
    normalize_mcp_servers,
)


DEFAULT_OPENCODE_CONFIG_PATH = Path(".codepilot") / "mcp" / "opencode.json"


def build_launch_plan(
    *,
    executable: str,
    prompt: str = "",
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None = None,
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> LaunchPlan:
    target_path = str(config_path or DEFAULT_OPENCODE_CONFIG_PATH)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": _opencode_servers(normalize_mcp_servers(mcp_servers)),
    }
    command = [executable, "run"]
    if prompt:
        command.append(prompt)
    merged_env = dict(env or {})
    merged_env["OPENCODE_CONFIG"] = target_path
    return LaunchPlan(
        agent="opencode",
        command=command,
        env=merged_env,
        mcp_config=config,
        config_files={target_path: json_config_text(config)},
    )


def _opencode_servers(servers: list[MCPServerSpec]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.url:
            item: dict[str, Any] = {
                "type": "remote",
                "url": server.url,
                "enabled": server.enabled,
            }
            if server.headers:
                item["headers"] = dict(server.headers)
        else:
            item = {
                "type": "local",
                "command": [server.command, *server.args],
                "enabled": server.enabled,
            }
            if server.env:
                item["environment"] = dict(server.env)
        result[server.name] = item
    return result
