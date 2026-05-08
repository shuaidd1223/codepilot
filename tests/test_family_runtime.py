from __future__ import annotations

import pytest

from codepilot.ai_support.family_runtime import (
    FamilyRuntimeUnavailable,
    build_env_for_family,
)
from codepilot.ai_support.cli_families import get_family
from codepilot.core.config import AgentsConfig


def _cfg(providers: dict[str, dict[str, str | bool | int]]) -> AgentsConfig:
    return AgentsConfig.from_dict({"providers": providers})


def test_cli_families_declare_env_bridge_config():
    claude = get_family("claude")
    codex = get_family("codex")
    opencode = get_family("opencode")

    assert claude is not None
    assert claude.env_bridge is not None
    assert claude.env_bridge.target_var == "ANTHROPIC_API_KEY"
    assert set(claude.env_bridge.source_providers) == {
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
    }

    assert codex is not None
    assert codex.env_bridge is not None
    assert codex.env_bridge.target_var == "OPENAI_API_KEY"
    assert set(codex.env_bridge.source_providers) == {
        "openai-gpt4o",
        "openai-gpt4",
        "openai-gpt35",
    }

    assert opencode is not None
    assert opencode.env_bridge is not None
    assert opencode.env_bridge.source_providers == "smart_pick"


def test_native_auth_file_takes_priority_over_config_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = _cfg({"claude-sonnet": {"api_key": "sk-should-not-inject"}})

    assert build_env_for_family("claude", cfg) == {}


def test_claude_and_codex_use_their_target_env_vars(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    claude_env = build_env_for_family(
        "claude",
        _cfg({"claude-sonnet": {"api_key": "sk-ant-test"}}),
    )
    codex_env = build_env_for_family(
        "codex",
        _cfg({"openai-gpt4o": {"api_key": "sk-oa-test"}}),
    )

    assert claude_env == {"ANTHROPIC_API_KEY": "sk-ant-test"}
    assert codex_env == {"OPENAI_API_KEY": "sk-oa-test"}


def test_opencode_uses_existing_smart_pick_order(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = _cfg(
        {
            "claude-sonnet": {"api_key": "sk-ant-test"},
            "deepseek": {
                "api_key": "sk-ds-test",
                "base_url": "https://api.deepseek.com",
            },
            "openai-gpt4o": {"api_key": "sk-oa-test"},
        }
    )

    env = build_env_for_family("opencode", cfg)

    assert env == {"ANTHROPIC_API_KEY": "sk-ant-test"}


def test_opencode_native_auth_file_suppresses_config_key_injection(tmp_path, monkeypatch):
    home = tmp_path / "home"
    auth_file = home / ".local" / "share" / "opencode" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = _cfg({"deepseek": {"api_key": "sk-ds-test", "base_url": "https://api.deepseek.com"}})

    assert build_env_for_family("opencode", cfg) == {}


def test_missing_key_without_native_auth_raises_clear_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    with pytest.raises(FamilyRuntimeUnavailable) as excinfo:
        build_env_for_family("codex", _cfg({}))

    message = str(excinfo.value)
    assert "codex" in message
    assert "OPENAI_API_KEY" in message
    assert "openai-gpt4o" in message
