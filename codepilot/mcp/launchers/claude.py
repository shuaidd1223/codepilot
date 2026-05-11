"""Claude Code MCP launch-plan builder."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from codepilot.mcp.launchers import (
    LaunchPlan,
    MCPServerSpec,
    json_config_text,
    normalize_mcp_servers,
)
from codepilot.mcp.launchers.language import with_chinese_interaction_instructions


def build_launch_plan(
    *,
    executable: str,
    prompt: str = "",
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None = None,
    env: Mapping[str, str] | None = None,
) -> LaunchPlan:
    config = {"mcpServers": _claude_servers(normalize_mcp_servers(mcp_servers))}
    config_args = ["--mcp-config", json_config_text(config), "--strict-mcp-config"]
    prompt_text = with_chinese_interaction_instructions(prompt)
    command = [
        executable,
        *config_args,
        "-p",
        prompt_text,
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ]
    return LaunchPlan(
        agent="claude",
        command=command,
        env=dict(env or {}),
        mcp_config=config,
        config_args=config_args,
    )


def _claude_servers(servers: list[MCPServerSpec]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.url:
            item: dict[str, Any] = {"type": "http", "url": server.url}
            if server.headers:
                item["headers"] = dict(server.headers)
        else:
            item = {"command": server.command}
            if server.args:
                item["args"] = list(server.args)
            if server.env:
                item["env"] = dict(server.env)
        result[server.name] = item
    return result
