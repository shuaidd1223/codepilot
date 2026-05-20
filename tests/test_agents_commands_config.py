"""Unit tests for the new [agents.commands] map and related schema additions.

This file is the Red half of the TDD slice for task #209: migrating from
``[agents] codex_cmd / claude_cmd`` scalars to a ``[agents.commands]`` map,
adding ``[automation] fallback_cli_order`` and ``[providers.opencode]``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from codepilot.core.config import (
    DEFAULT_AGENT_COMMANDS,
    DEFAULT_FALLBACK_CLI_ORDER,
    AgentsConfig,
    ConfigError,
)


def test_agents_commands_map_parses_all_three_families():
    cfg = AgentsConfig.from_dict(
        {
            "agents": {
                "commands": {
                    "claude": "claude",
                    "codex": "codex-cli",
                    "opencode": "/opt/opencode/bin/opencode",
                },
            }
        }
    )

    assert cfg.commands["claude"] == "claude"
    assert cfg.commands["codex"] == "codex-cli"
    assert cfg.commands["opencode"] == "/opt/opencode/bin/opencode"


def test_agents_commands_defaults_when_table_missing():
    cfg = AgentsConfig.from_dict({})

    for family, default in DEFAULT_AGENT_COMMANDS.items():
        assert cfg.commands[family] == default


def test_agents_commands_partial_override_keeps_defaults_for_others():
    cfg = AgentsConfig.from_dict(
        {"agents": {"commands": {"codex": "/usr/local/bin/codex"}}}
    )

    assert cfg.commands["codex"] == "/usr/local/bin/codex"
    assert cfg.commands["claude"] == "claude"
    assert cfg.commands["opencode"] == "cp-opencode"


def test_legacy_codex_cmd_raises_config_error_with_migration_example():
    with pytest.raises(ConfigError) as excinfo:
        AgentsConfig.from_dict({"agents": {"codex_cmd": "codex"}})

    msg = str(excinfo.value)
    assert "codex_cmd" in msg
    assert "[agents.commands]" in msg
    assert "fallback_cli_order" in msg


def test_legacy_claude_cmd_raises_config_error():
    with pytest.raises(ConfigError):
        AgentsConfig.from_dict({"agents": {"claude_cmd": "claude"}})


def test_command_for_returns_configured_path():
    cfg = AgentsConfig.from_dict(
        {"agents": {"commands": {"claude": "/usr/local/bin/claude"}}}
    )
    assert cfg.command_for("claude") == "/usr/local/bin/claude"


def test_command_for_unknown_family_falls_back_to_family_name():
    cfg = AgentsConfig.from_dict({})
    assert cfg.command_for("not-a-known-family") == "not-a-known-family"


def test_default_fallback_cli_order_includes_opencode_last():
    cfg = AgentsConfig.from_dict({})
    assert cfg.automation.fallback_cli_order == DEFAULT_FALLBACK_CLI_ORDER
    assert DEFAULT_FALLBACK_CLI_ORDER[-1] == "opencode"


def test_custom_fallback_cli_order_is_respected():
    cfg = AgentsConfig.from_dict(
        {"automation": {"fallback_cli_order": ["opencode", "codex"]}}
    )
    assert cfg.automation.fallback_cli_order == ["opencode", "codex"]


def test_agent_language_defaults_to_english():
    cfg = AgentsConfig.from_dict({})

    assert cfg.automation.agent_language == "en"


def test_repository_agents_toml_sets_agent_language_to_chinese():
    config_path = Path(__file__).resolve().parents[1] / "AGENTS.toml"
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    cfg = AgentsConfig.from_dict(data, config_file_path=str(config_path))

    assert cfg.automation.agent_language == "zh-CN"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("en-US", "en"),
        ("english", "en"),
        ("zh", "zh-CN"),
        ("zh-CN", "zh-CN"),
        ("中文", "zh-CN"),
    ],
)
def test_agent_language_aliases_are_normalized(raw: str, expected: str):
    cfg = AgentsConfig.from_dict({"automation": {"agent_language": raw}})

    assert cfg.automation.agent_language == expected


def test_invalid_agent_language_raises_actionable_config_error():
    with pytest.raises(ConfigError) as excinfo:
        AgentsConfig.from_dict({"automation": {"agent_language": "fr"}})

    message = str(excinfo.value)
    assert "automation.agent_language" in message
    assert "en" in message
    assert "zh-CN" in message


def test_providers_opencode_section_parses_like_other_providers():
    cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "opencode": {
                    "api_key": "test-key",
                    "model": "auto",
                    "base_url": "https://example.invalid",
                }
            }
        }
    )

    assert "opencode" in cfg.providers
    assert cfg.providers["opencode"].api_key == "test-key"
    assert cfg.providers["opencode"].model == "auto"
    assert cfg.providers["opencode"].base_url == "https://example.invalid"


def test_legacy_field_error_lists_actionable_replacement():
    with pytest.raises(ConfigError) as excinfo:
        AgentsConfig.from_dict({"agents": {"claude_cmd": "claude"}})

    msg = str(excinfo.value)
    assert "claude" in msg
    assert "codex" in msg
    assert "opencode" in msg
