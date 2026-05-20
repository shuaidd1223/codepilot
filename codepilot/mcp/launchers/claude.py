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
from codepilot.mcp.launchers.language import (
    interaction_instructions,
    with_interaction_instructions,
)


def build_launch_plan(
    *,
    executable: str,
    prompt: str = "",
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None = None,
    env: Mapping[str, str] | None = None,
    session: str | None = None,
    language: str = "en",
) -> LaunchPlan:
    config = {"mcpServers": _claude_servers(normalize_mcp_servers(mcp_servers))}
    config_args = ["--mcp-config", json_config_text(config), "--strict-mcp-config"]
    session_id = str(session or "").strip()
    base = [executable, *config_args, "--dangerously-skip-permissions"]
    if prompt:
        # Headless one-shot via --print.
        prompt_text = with_interaction_instructions(prompt, language=language)
        command = [
            *base,
            "-p",
            prompt_text,
            "--output-format",
            "text",
        ]
    else:
        # Interactive TUI. --append-system-prompt silently extends the system prompt
        # so Claude responds in Chinese without producing a visible turn on launch.
        command = [
            *base,
            "--append-system-prompt",
            interaction_instructions(language),
        ]
        if session_id:
            command.extend(["--resume", session_id])
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
