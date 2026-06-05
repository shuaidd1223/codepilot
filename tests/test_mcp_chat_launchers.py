from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codepilot.mcp.launchers.language import (
    CHINESE_INTERACTION_INSTRUCTIONS,
    ENGLISH_INTERACTION_INSTRUCTIONS,
)
from codepilot.mcp.launchers import UnsupportedAgentError, build_mcp_launch_plan
from codepilot.opencode.config import OpenCodeConfig


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
            f"{ENGLISH_INTERACTION_INSTRUCTIONS}\n\nUser request:\nCheck project health.",
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
        "--plugin-dir",
        plan.command[6],
        "--append-system-prompt",
        ENGLISH_INTERACTION_INSTRUCTIONS,
    ]
    assert "-p" not in plan.command


def test_claude_launcher_registers_task_slash_command_plugin(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "claude",
        executable="claude-bin",
        mcp_servers=_server(tmp_path),
    )

    plugin_index = plan.command.index("--plugin-dir") + 1
    plugin_dir = Path(plan.command[plugin_index])
    assert plugin_dir.name == "claude-codepilot"
    assert str(plugin_dir / "commands" / "task.md") in plan.config_files
    command_doc = plan.config_files[str(plugin_dir / "commands" / "task.md")]
    assert "CodePilot TASK MODE" in command_doc
    assert "codepilot_pipeline(requirement=$ARGUMENTS)" in command_doc
    assert "Do NOT read, write, edit, patch, or inspect repository files directly" in command_doc


def test_claude_launcher_with_session_resumes_interactive_tui(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "claude",
        executable="claude-bin",
        mcp_servers=_server(tmp_path),
        session="ses-abc",
    )

    assert "--plugin-dir" in plan.command
    assert plan.command[-2:] == ["--resume", "ses-abc"]
    assert "--append-system-prompt" not in plan.command
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
        "mcp_servers.filesystem.command='node'",
        "-c",
        "mcp_servers.filesystem.args=['server.js']",
        "-c",
        f"mcp_servers.filesystem.env={{ROOT='{str(tmp_path)}'}}",
    ]
    assert plan.command[-5:-1] == [
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    assert plan.command[-1] == f"{ENGLISH_INTERACTION_INSTRUCTIONS}\n\nUser request:\nCheck project health."
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


def test_codex_launcher_without_prompt_starts_interactive_tui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "which", lambda value: None)

    plan = build_mcp_launch_plan(
        "codex",
        executable="codex-bin",
        mcp_servers=_server(tmp_path),
    )

    assert plan.agent == "codex"
    assert plan.command[1:3] == ["--profile", "codepilot"]
    # Same -c overrides as headless mode...
    assert plan.command[3:9] == [
        "-c",
        "mcp_servers.filesystem.command='node'",
        "-c",
        "mcp_servers.filesystem.args=['server.js']",
        "-c",
        f"mcp_servers.filesystem.env={{ROOT='{str(tmp_path)}'}}",
    ]
    # ...but no `exec` subcommand and no positional prompt.
    assert "exec" not in plan.command
    assert plan.command[0] == "codex-bin"


def test_codex_launcher_registers_task_slash_command_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(shutil, "which", lambda value: None)
    monkeypatch.setenv("CODEPILOT_HOME", str(tmp_path / ".codepilot"))
    codex_home = tmp_path / ".codex"

    plan = build_mcp_launch_plan(
        "codex",
        executable="codex-bin",
        mcp_servers=_server(tmp_path),
        env={"CODEX_HOME": str(codex_home)},
    )

    assert plan.command[1:3] == ["--profile", "codepilot"]
    profile_path = codex_home / "codepilot.config.toml"
    assert str(profile_path) in plan.config_files
    profile = plan.config_files[str(profile_path)]
    assert "[marketplaces.codepilot]" in profile
    assert '[plugins."codepilot@codepilot"]' in profile
    assert "enabled = true" in profile

    plugin_root = tmp_path / ".codepilot" / "codex" / "marketplace" / "plugins" / "codepilot"
    cache_root = codex_home / "plugins" / "cache" / "codepilot" / "codepilot"
    command_paths = [
        Path(path)
        for path in plan.config_files
        if Path(path).name == "task.md"
    ]
    assert plugin_root / "commands" / "task.md" in command_paths
    assert any(path.parent.parent.parent == cache_root for path in command_paths)
    task_doc = plan.config_files[str(plugin_root / "commands" / "task.md")]
    assert "# /task" in task_doc
    assert "codepilot_pipeline(requirement=$ARGUMENTS)" in task_doc
    assert "Do NOT read, write, edit, patch, or inspect repository files directly" in task_doc


def test_codex_launcher_bypasses_npm_cmd_wrapper_for_interactive_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(shutil, "which", lambda value: None)

    codex_cmd = tmp_path / "codex.cmd"
    codex_js = tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node_exe = tmp_path / "node.exe"
    codex_cmd.write_text("@echo off\n", encoding="utf-8")
    codex_js.parent.mkdir(parents=True)
    codex_js.write_text("", encoding="utf-8")
    node_exe.write_text("", encoding="utf-8")

    plan = build_mcp_launch_plan(
        "codex",
        executable=str(codex_cmd),
        mcp_servers=_server(tmp_path),
    )

    assert plan.command[:2] == [str(node_exe), str(codex_js)]
    assert plan.command[2:4] == ["--profile", "codepilot"]
    assert "-c" in plan.command
    assert "exec" not in plan.command


def test_codex_launcher_prefers_native_windows_exe_for_interactive_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    codex_cmd = tmp_path / "codex.cmd"
    codex_js = tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    native_exe = tmp_path / "native" / "codex.exe"
    codex_cmd.write_text("@echo off\n", encoding="utf-8")
    codex_js.parent.mkdir(parents=True)
    codex_js.write_text("", encoding="utf-8")
    native_exe.parent.mkdir()
    native_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(shutil, "which", lambda value: str(native_exe) if value == "codex.exe" else None)

    plan = build_mcp_launch_plan(
        "codex",
        executable=str(codex_cmd),
        mcp_servers=_server(tmp_path),
    )

    assert plan.command[0] == str(native_exe)
    assert plan.command[1:3] == ["--profile", "codepilot"]
    assert "codex.js" not in plan.command
    assert "exec" not in plan.command


def test_codex_launcher_with_session_resumes_interactive_tui(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "codex",
        executable="codex-bin",
        mcp_servers=_server(tmp_path),
        session="ses-xyz",
    )

    assert "--profile" in plan.command
    assert plan.command[-2:] == ["resume", "ses-xyz"]
    assert "exec" not in plan.command


def test_opencode_launcher_uses_config_file_env_injection(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("codepilot.opencode.paths.global_storage_root", lambda: tmp_path / ".codepilot")
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
    runtime_root = tmp_path / ".codepilot" / "opencode" / "default"
    assert plan.env == {
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_TUI_CONFIG": str(runtime_root / "tui.json"),
        "OPENCODE_CONFIG_DIR": str(runtime_root / "config"),
        "XDG_DATA_HOME": str(runtime_root / "xdg-data"),
        "XDG_CACHE_HOME": str(runtime_root / "xdg-cache"),
        "XDG_STATE_HOME": str(runtime_root / "xdg-state"),
        "OPENCODE_DISABLE_TERMINAL_TITLE": "1",
        "CODEPILOT_OPENCODE_BRAND_NAME": "CodePilot",
    }
    assert str(config_path) in plan.config_files
    assert str(runtime_root / "tui.json") in plan.config_files
    assert str(runtime_root / "config" / "agents" / "codepilot.md") in plan.config_files
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


def test_opencode_launcher_with_session_starts_codepilot_profile_session(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("codepilot.opencode.paths.global_storage_root", lambda: tmp_path / ".codepilot")
    config_path = tmp_path / "opencode.mcp.json"

    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        mcp_servers=_server(tmp_path),
        config_path=config_path,
        opencode_session="ses_123",
    )

    runtime_root = tmp_path / ".codepilot" / "opencode" / "default"
    assert plan.command == ["opencode-bin", "--agent", "codepilot", "-s", "ses_123"]
    assert plan.env["OPENCODE_CONFIG"] == str(config_path)
    assert plan.env["XDG_DATA_HOME"] == str(runtime_root / "xdg-data")


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
    assert "Task Status" in payload["command"]


def test_mcp_launchers_can_inject_chinese_interaction_rules(tmp_path: Path):
    claude = build_mcp_launch_plan(
        "claude",
        executable="claude-bin",
        prompt="Check project health.",
        mcp_servers=_server(tmp_path),
        language="zh-CN",
    )
    codex = build_mcp_launch_plan(
        "codex",
        executable="codex-bin",
        prompt="Check project health.",
        mcp_servers=_server(tmp_path),
        language="zh-CN",
    )

    assert claude.command[-3] == f"{CHINESE_INTERACTION_INSTRUCTIONS}\n\n用户请求：\nCheck project health."
    assert codex.command[-1] == f"{CHINESE_INTERACTION_INSTRUCTIONS}\n\n用户请求：\nCheck project health."


def test_opencode_launcher_language_overrides_unset_tool_config_language(tmp_path: Path):
    config_path = tmp_path / "opencode.json"

    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        mcp_servers=_server(tmp_path),
        config_path=config_path,
        opencode_config=OpenCodeConfig(),
        language="zh-CN",
    )

    payload = json.loads(plan.config_files[str(config_path)])
    assert payload["instructions"][0].endswith("codepilot.zh-CN.md")
    assert "任务状态" in payload["command"]
    assert "Task Status" not in payload["command"]


def test_launcher_rejects_unknown_agent_name(tmp_path: Path):
    with pytest.raises(UnsupportedAgentError, match="Unsupported MCP launcher agent"):
        build_mcp_launch_plan(
            "ghost",
            executable="ghost-bin",
            prompt="Check project health.",
            mcp_servers=_server(tmp_path),
        )
