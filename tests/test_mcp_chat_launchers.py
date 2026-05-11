from __future__ import annotations

import json
from pathlib import Path

import pytest

from codepilot.mcp.launchers.language import CHINESE_INTERACTION_INSTRUCTIONS
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
        "--dangerously-skip-permissions",
        "-p",
        f"{CHINESE_INTERACTION_INSTRUCTIONS}\n\n用户请求：\nCheck project health.",
        "--output-format",
        "text",
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


def test_claude_launcher_without_prompt_starts_interactive_tui(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "claude",
        executable="claude-bin",
        mcp_servers=_server(tmp_path),
    )

    assert plan.agent == "claude"
    assert plan.command == [
        "claude-bin",
        "--mcp-config",
        plan.config_args[1],
        "--strict-mcp-config",
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        CHINESE_INTERACTION_INSTRUCTIONS,
    ]
    assert "-p" not in plan.command


def test_claude_launcher_with_session_resumes_interactive_tui(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "claude",
        executable="claude-bin",
        mcp_servers=_server(tmp_path),
        session="ses-abc",
    )

    assert plan.command[-2:] == ["--resume", "ses-abc"]
    assert "--append-system-prompt" in plan.command
    assert "-p" not in plan.command


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
    assert plan.command[-5:-1] == [
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    assert plan.command[-1] == f"{CHINESE_INTERACTION_INSTRUCTIONS}\n\n用户请求：\nCheck project health."
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


def test_codex_launcher_without_prompt_starts_interactive_tui(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "codex",
        executable="codex-bin",
        mcp_servers=_server(tmp_path),
    )

    assert plan.agent == "codex"
    # Same -c overrides as headless mode...
    assert plan.command[1:7] == [
        "-c",
        'mcp_servers.filesystem.command="node"',
        "-c",
        'mcp_servers.filesystem.args=["server.js"]',
        "-c",
        f'mcp_servers.filesystem.env={{ROOT={json.dumps(str(tmp_path))}}}',
    ]
    # ...but no `exec` subcommand and no positional prompt.
    assert "exec" not in plan.command
    assert plan.command[0] == "codex-bin"


def test_codex_launcher_with_session_resumes_interactive_tui(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "codex",
        executable="codex-bin",
        mcp_servers=_server(tmp_path),
        session="ses-xyz",
    )

    assert plan.command[-2:] == ["resume", "ses-xyz"]
    assert "exec" not in plan.command


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
    assert plan.command == ["opencode-bin", "run", "--agent", "codepilot", "Check project health."]
    assert plan.env == {
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_TUI_CONFIG": str(tmp_path / "tui.json"),
        "OPENCODE_CONFIG_DIR": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "OPENCODE_DISABLE_TERMINAL_TITLE": "1",
        "CODEPILOT_OPENCODE_BRAND_NAME": "CodePilot",
    }
    assert str(config_path) in plan.config_files
    assert str(tmp_path / "tui.json") in plan.config_files
    assert str(tmp_path / "config" / "agents" / "codepilot.md") in plan.config_files
    payload = json.loads(plan.config_files[str(config_path)])
    assert payload["mcp"] == {
        "filesystem": {
            "type": "local",
            "command": ["node", "server.js"],
            "enabled": True,
            "environment": {"ROOT": str(tmp_path)},
        }
    }
    assert payload["default_agent"] == "codepilot"
    assert payload["permission"]["bash"] == "ask"


def test_opencode_launcher_without_prompt_starts_interactive_cli(tmp_path: Path):
    config_path = tmp_path / "opencode.mcp.json"

    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        mcp_servers=_server(tmp_path),
        config_path=config_path,
    )

    assert plan.command == ["opencode-bin", "--agent", "codepilot"]


def test_opencode_launcher_with_session_starts_codepilot_profile_session(tmp_path: Path):
    config_path = tmp_path / "opencode.mcp.json"

    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        mcp_servers=_server(tmp_path),
        config_path=config_path,
        opencode_session="ses_123",
    )

    assert plan.command == ["opencode-bin", "--agent", "codepilot", "-s", "ses_123"]
    assert plan.env["OPENCODE_CONFIG"] == str(config_path)
    assert plan.env["XDG_DATA_HOME"] == str(tmp_path / "xdg-data")


def test_opencode_launcher_run_with_session_uses_json_resume_flag(tmp_path: Path):
    config_path = tmp_path / "opencode.mcp.json"

    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        prompt="继续处理",
        mcp_servers=_server(tmp_path),
        config_path=config_path,
        opencode_session="ses_123",
    )

    assert plan.command == [
        "opencode-bin",
        "run",
        "--agent",
        "codepilot",
        "--session",
        "ses_123",
        "继续处理",
    ]


def test_opencode_launcher_without_mcp_still_uses_config_file():
    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        mcp_servers=None,
    )

    assert plan.command == ["opencode-bin", "--agent", "codepilot"]
    config_path = str(Path.home() / ".codepilot" / "opencode" / "default" / "opencode.json")
    assert plan.env == {
        "OPENCODE_CONFIG": config_path,
        "OPENCODE_TUI_CONFIG": str(Path.home() / ".codepilot" / "opencode" / "default" / "tui.json"),
        "OPENCODE_CONFIG_DIR": str(Path.home() / ".codepilot" / "opencode" / "default" / "config"),
        "XDG_DATA_HOME": str(Path.home() / ".codepilot" / "opencode" / "default" / "xdg-data"),
        "XDG_CACHE_HOME": str(Path.home() / ".codepilot" / "opencode" / "default" / "xdg-cache"),
        "XDG_STATE_HOME": str(Path.home() / ".codepilot" / "opencode" / "default" / "xdg-state"),
        "OPENCODE_DISABLE_TERMINAL_TITLE": "1",
        "CODEPILOT_OPENCODE_BRAND_NAME": "CodePilot",
    }
    payload = json.loads(plan.config_files[config_path])
    assert payload["mcp"] == {}
    assert payload["default_agent"] == "codepilot"
    assert "任务状态" in payload["command"]


def test_launcher_rejects_unknown_agent_name(tmp_path: Path):
    with pytest.raises(UnsupportedAgentError, match="Unsupported MCP launcher agent"):
        build_mcp_launch_plan(
            "ghost",
            executable="ghost-bin",
            prompt="Check project health.",
            mcp_servers=_server(tmp_path),
        )
