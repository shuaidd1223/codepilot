from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.ai_support.main_execute import execute_task_content_call
from codepilot.ai_support.main_resolution import (
    ResolvedAPITaskContentCall,
    ResolvedCLITaskContentCall,
)


def test_execute_task_content_call_api_applies_override_and_runs_provider():
    class Provider:
        api_key = "registry-key"

    captured = {}

    def _fake_resolve_api(provider_key, provider_ref):
        captured["provider_key"] = provider_key
        captured["provider_ref"] = provider_ref
        return Provider()

    def _fake_run_api(provider, prompt):
        captured["api_key"] = provider.api_key
        captured["prompt"] = prompt
        return "generated"

    result = execute_task_content_call(
        ResolvedAPITaskContentCall(
            provider_key="openai-gpt4",
            provider_ref="D:/cfg/AGENTS.toml",
            prompt="plan",
            api_key_override="sk-custom",
        ),
        resolve_api_provider=_fake_resolve_api,
        resolve_cli_provider=lambda *_args, **_kwargs: None,
        run_api_provider=_fake_run_api,
        run_cli_provider=lambda *_args, **_kwargs: "",
        get_node_modules_path=lambda: "",
    )

    assert result == "generated"
    assert captured["provider_key"] == "openai-gpt4"
    assert captured["provider_ref"] == "D:/cfg/AGENTS.toml"
    assert captured["api_key"] == "sk-custom"
    assert captured["prompt"] == "plan"


def test_execute_task_content_call_cli_runs_with_env_overrides():
    class Provider:
        name = "OpenAI Codex"
        cmd = "codex"

    captured = {}

    def _fake_run_cli(provider, prompt, env_overrides=None):
        captured["provider"] = provider.name
        captured["prompt"] = prompt
        captured["env_overrides"] = env_overrides
        return "ok"

    result = execute_task_content_call(
        ResolvedCLITaskContentCall(
            provider_key="codex",
            provider_ref="D:/project",
            prompt="build",
            env_overrides={"OPENAI_GPT4_API_KEY": "x"},
        ),
        resolve_api_provider=lambda *_args, **_kwargs: None,
        resolve_cli_provider=lambda *_args, **_kwargs: Provider(),
        run_api_provider=lambda *_args, **_kwargs: "",
        run_cli_provider=_fake_run_cli,
        get_node_modules_path=lambda: "",
    )

    assert result == "ok"
    assert captured["provider"] == "OpenAI Codex"
    assert captured["prompt"] == "build"
    assert captured["env_overrides"] == {"OPENAI_GPT4_API_KEY": "x"}


def test_execute_task_content_call_claude_node_requires_installed_cli(tmp_path):
    class Provider:
        name = "Claude Code (Node)"
        cmd = "node"

    with pytest.raises(RuntimeError, match="未找到 Claude Code CLI"):
        execute_task_content_call(
            ResolvedCLITaskContentCall(
                provider_key="claude-node",
                provider_ref="D:/project",
                prompt="build",
                env_overrides={},
            ),
            resolve_api_provider=lambda *_args, **_kwargs: None,
            resolve_cli_provider=lambda *_args, **_kwargs: Provider(),
            run_api_provider=lambda *_args, **_kwargs: "",
            run_cli_provider=lambda *_args, **_kwargs: "should-not-run",
            get_node_modules_path=lambda: str(Path(tmp_path)),
        )


