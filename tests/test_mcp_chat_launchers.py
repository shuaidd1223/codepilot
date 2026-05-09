from __future__ import annotations

import json
from pathlib import Path

import pytest

from codepilot.mcp.launchers import UnsupportedAgentError, build_mcp_launch_plan


def _server(tmp_path: Path) -> dict[str, dict[str, object]]:
    return {
        "filesystem": {
            "command": "node",
            "args": ["server.js"],
            "env": {"ROOT": str(tmp_path)},
        }
    }


def test_claude_launcher_injects_mcp_config_with_cli_flag(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "claude",
        executable="claude-bin",
        prompt="Check project health.",
        mcp_servers=_server(tmp_path),
    )

    assert plan.agent == "claude"
    assert plan.command == [
        "claude-bin",
        "--mcp-config",
        plan.config_args[1],
        "--strict-mcp-config",
        "-p",
        "Check project health.",
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ]
    assert plan.env == {}
    payload = json.loads(plan.config_args[1])
    assert payload == {
        "mcpServers": {
            "filesystem": {
                "command": "node",
                "args": ["server.js"],
                "env": {"ROOT": str(tmp_path)},
            }
        }
    }


def test_codex_launcher_injects_mcp_servers_with_config_overrides(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "codex",
        executable="codex-bin",
        prompt="Check project health.",
        mcp_servers=_server(tmp_path),
    )

    assert plan.agent == "codex"
    assert plan.command[:7] == [
        "codex-bin",
        "-c",
        'mcp_servers.filesystem.command="node"',
        "-c",
        'mcp_servers.filesystem.args=["server.js"]',
        "-c",
        f'mcp_servers.filesystem.env={{ROOT={json.dumps(str(tmp_path))}}}',
    ]
    assert plan.command[-5:] == [
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
        "Check project health.",
    ]
    assert plan.mcp_config == {
        "mcp_servers": {
            "filesystem": {
                "command": "node",
                "args": ["server.js"],
                "env": {"ROOT": str(tmp_path)},
                "enabled": True,
            }
        }
    }


def test_opencode_launcher_uses_config_file_env_injection(tmp_path: Path):
    config_path = tmp_path / "opencode.mcp.json"

    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        prompt="Check project health.",
        mcp_servers=_server(tmp_path),
        config_path=config_path,
    )

    assert plan.agent == "opencode"
    assert plan.command == ["opencode-bin", "run", "Check project health."]
    assert plan.env == {"OPENCODE_CONFIG": str(config_path)}
    assert set(plan.config_files) == {str(config_path)}
    payload = json.loads(plan.config_files[str(config_path)])
    assert payload == {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "filesystem": {
                "type": "local",
                "command": ["node", "server.js"],
                "enabled": True,
                "environment": {"ROOT": str(tmp_path)},
            }
        },
    }


def test_opencode_launcher_without_prompt_starts_interactive_cli(tmp_path: Path):
    config_path = tmp_path / "opencode.mcp.json"

    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        mcp_servers=_server(tmp_path),
        config_path=config_path,
    )

    assert plan.command == ["opencode-bin"]


def test_launcher_rejects_unknown_agent_name(tmp_path: Path):
    with pytest.raises(UnsupportedAgentError, match="Unsupported MCP launcher agent"):
        build_mcp_launch_plan(
            "ghost",
            executable="ghost-bin",
            prompt="Check project health.",
            mcp_servers=_server(tmp_path),
        )
