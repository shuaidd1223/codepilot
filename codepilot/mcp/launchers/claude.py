"""Claude Code MCP launch-plan builder."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from codepilot.core.paths import global_storage_root
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
    plugin_files = _codepilot_plugin_files()
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
        # Interactive TUI.
        plugin_args = ["--plugin-dir", str(_codepilot_plugin_dir())]
        if session_id:
            # Resuming an existing session — the system prompt is already
            # baked into the transcript, so we must NOT append it again.
            command = [*base, *plugin_args, "--resume", session_id]
        else:
            # --append-system-prompt silently extends the system prompt
            # so Claude responds in Chinese without producing a visible turn on launch.
            command = [
                *base,
                *plugin_args,
                "--append-system-prompt",
                interaction_instructions(language),
            ]
    return LaunchPlan(
        agent="claude",
        command=command,
        env=dict(env or {}),
        mcp_config=config,
        config_args=config_args,
        config_files=plugin_files,
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


def _codepilot_plugin_dir() -> Path:
    return global_storage_root() / "claude-codepilot"


def _codepilot_plugin_files() -> dict[str, str]:
    plugin_dir = _codepilot_plugin_dir()
    return {
        str(plugin_dir / ".claude-plugin" / "plugin.json"): (
            '{\n'
            '  "name": "codepilot",\n'
            '  "description": "CodePilot workflow slash commands for Claude Code"\n'
            '}\n'
        ),
        str(plugin_dir / "commands" / "task.md"): _task_command_markdown(),
    }


def _task_command_markdown() -> str:
    return """---
description: Run a requirement through the CodePilot MCP pipeline in tool-only mode
argument-hint: <requirement>
allowed-tools: [mcp__codepilot__pipeline, mcp__codepilot__run_once, mcp__codepilot__workflow_next, mcp__codepilot__workflow_status, mcp__codepilot__daemon_status, mcp__codepilot__create_task, mcp__codepilot__generate_breakdown]
---

# CodePilot TASK MODE

The user invoked `/task` with this requirement:
$ARGUMENTS

Hard constraints:

- Use ONLY CodePilot MCP tools for this request.
- Do NOT read, write, edit, patch, or inspect repository files directly.
- Do NOT run shell commands or implement code yourself.
- First call codepilot_pipeline(requirement=$ARGUMENTS).
- If codepilot_pipeline cannot complete, use only CodePilot MCP fallback tools such as codepilot_run_once, codepilot_workflow_next(auto=true), codepilot_workflow_status, codepilot_daemon_status, codepilot_create_task, and codepilot_generate_breakdown.
- Monitor tool results and report task ids, failures, manual follow-up commands, and validation results returned by the tools.
"""
