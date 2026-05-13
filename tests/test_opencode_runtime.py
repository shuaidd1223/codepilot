"""Unit tests for codepilot.ai_support.opencode_runtime (task #211).

Covers:
  * Smart backend selection: anthropic > deepseek (openai-compat) > openai
  * Clear error when no provider key is configured
  * No leak of unrelated provider keys into the OpenCode subprocess env
  * Schema-prompt wrapper bakes JSON instructions into the prompt
"""

from __future__ import annotations

import pytest

from codepilot.ai_support.opencode_runtime import (
    OpenCodeBackend,
    OpenCodeBackendUnavailable,
    build_opencode_env,
    select_opencode_backend,
    wrap_schema_prompt,
)
from codepilot.core.config import AgentsConfig


def _cfg(providers: dict[str, dict[str, str | bool | int]]) -> AgentsConfig:
    return AgentsConfig.from_dict({"providers": providers})


def test_anthropic_takes_priority_when_claude_provider_has_key():
    cfg = _cfg(
        {
            "claude-sonnet": {"api_key": "sk-ant-test", "model": "claude-sonnet-4"},
            "deepseek": {"api_key": "sk-ds-test", "base_url": "https://api.deepseek.com"},
        }
    )

    backend = select_opencode_backend(cfg)

    assert backend.name == OpenCodeBackend.ANTHROPIC.name
    env = build_opencode_env(cfg)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    # Must not leak the deepseek key into the subprocess env.
    assert "OPENAI_API_KEY" not in env
    assert "DEEPSEEK_API_KEY" not in env


def test_deepseek_picked_when_no_anthropic_key():
    cfg = _cfg(
        {
            "deepseek": {
                "api_key": "sk-ds-test",
                "base_url": "https://api.deepseek.com",
            },
        }
    )

    backend = select_opencode_backend(cfg)

    assert backend.name == OpenCodeBackend.OPENAI_COMPATIBLE.name
    env = build_opencode_env(cfg)
    assert env["OPENAI_API_KEY"] == "sk-ds-test"
    assert env["OPENAI_BASE_URL"] == "https://api.deepseek.com"
    assert "ANTHROPIC_API_KEY" not in env


def test_openai_picked_when_no_other_providers():
    cfg = _cfg({"openai-gpt4o": {"api_key": "sk-oa-test"}})

    backend = select_opencode_backend(cfg)

    assert backend.name == OpenCodeBackend.OPENAI.name
    env = build_opencode_env(cfg)
    assert env["OPENAI_API_KEY"] == "sk-oa-test"
    # No base_url override for native OpenAI.
    assert "OPENAI_BASE_URL" not in env


def test_generic_openai_provider_is_accepted_for_opencode():
    cfg = _cfg(
        {
            "openai": {
                "api_key": "sk-oa-test",
                "base_url": "https://api.openai.com/v1",
            }
        }
    )

    backend = select_opencode_backend(cfg)

    assert backend.name == OpenCodeBackend.OPENAI.name
    assert backend.source_provider == "openai"
    env = build_opencode_env(cfg)
    assert env["OPENAI_API_KEY"] == "sk-oa-test"
    assert env["OPENAI_BASE_URL"] == "https://api.openai.com/v1"


def test_anthropic_priority_chain_falls_through_to_first_keyed_claude_provider():
    cfg = _cfg(
        {
            "claude-opus": {"api_key": ""},
            "claude-sonnet": {"api_key": ""},
            "claude-haiku": {"api_key": "sk-haiku"},
        }
    )

    env = build_opencode_env(cfg)

    assert env["ANTHROPIC_API_KEY"] == "sk-haiku"


def test_no_provider_key_raises_with_actionable_message():
    cfg = _cfg({})

    with pytest.raises(OpenCodeBackendUnavailable) as excinfo:
        select_opencode_backend(cfg)

    msg = str(excinfo.value)
    assert "OpenCode" in msg or "opencode" in msg
    # Must mention at least one configurable provider name to help the user.
    assert any(name in msg for name in ("anthropic", "deepseek", "openai"))


def test_disabled_provider_is_skipped_even_with_api_key():
    cfg = _cfg(
        {
            "claude-sonnet": {"api_key": "sk-ant", "enabled": False},
            "deepseek": {"api_key": "sk-ds", "base_url": "https://api.deepseek.com"},
        }
    )

    env = build_opencode_env(cfg)

    # The disabled anthropic provider must be ignored, falling through to deepseek.
    assert "ANTHROPIC_API_KEY" not in env
    assert env["OPENAI_API_KEY"] == "sk-ds"


def test_wrap_schema_prompt_includes_schema_and_user_prompt():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    wrapped = wrap_schema_prompt("回答用户问题", schema)

    assert "回答用户问题" in wrapped
    # The schema should be embedded so OpenCode knows the exact contract.
    assert "answer" in wrapped
    # Output must be JSON-only — make this explicit so the prompt is self-describing.
    assert "JSON" in wrapped or "json" in wrapped


def test_run_opencode_schema_prompt_is_callable_and_documented():
    """Smoke-check: the schema-prompt runner must be importable from the module."""
    from codepilot.ai_support import opencode_runtime

    assert callable(opencode_runtime.run_opencode_schema_prompt)
    assert opencode_runtime.run_opencode_schema_prompt.__doc__
