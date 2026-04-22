from __future__ import annotations

import pytest

from codepilot.ai_main_resolution import (
    ResolvedAPITaskContentCall,
    ResolvedCLITaskContentCall,
    resolve_task_content_call,
)


def test_resolve_task_content_call_dual_uses_codex_cli_and_builds_prompt():
    captured = {}

    def _fake_check(agent, project_path=None):
        captured["agent"] = agent
        captured["project_path"] = project_path
        return True, "ok"

    resolved = resolve_task_content_call(
        "实现自动重试",
        project_path="D:/project",
        agent="dual",
        config_ref="D:/cfg/AGENTS.toml",
        normalize_agent_name=lambda value: value,
        check_provider_availability=_fake_check,
        collect_project_context=lambda _: "ctx",
        task_prompt_template="{title}\n{project_context}",
        api_provider_keys={"openai-gpt4"},
        cli_provider_keys={"codex", "claude"},
    )

    assert isinstance(resolved, ResolvedCLITaskContentCall)
    assert resolved.provider_key == "codex"
    assert resolved.prompt == "实现自动重试\nctx"
    assert captured["agent"] == "dual"
    assert captured["project_path"] == "D:/cfg/AGENTS.toml"


def test_resolve_task_content_call_api_uses_override_key():
    resolved = resolve_task_content_call(
        "生成任务",
        project_path="D:/project",
        agent="gpt4",
        api_keys={"openai-gpt4": "sk-custom"},
        normalize_agent_name=lambda value: "openai-gpt4",
        check_provider_availability=lambda *_args, **_kwargs: (True, "ok"),
        collect_project_context=lambda _: "",
        task_prompt_template="{title}",
        api_provider_keys={"openai-gpt4"},
        cli_provider_keys={"codex"},
    )

    assert isinstance(resolved, ResolvedAPITaskContentCall)
    assert resolved.provider_key == "openai-gpt4"
    assert resolved.api_key_override == "sk-custom"


def test_resolve_task_content_call_raises_when_provider_unavailable():
    with pytest.raises(RuntimeError, match="不可用"):
        resolve_task_content_call(
            "生成任务",
            agent="codex",
            normalize_agent_name=lambda value: value,
            check_provider_availability=lambda *_args, **_kwargs: (False, "不可用"),
            collect_project_context=lambda _: "",
            task_prompt_template="{title}",
            api_provider_keys={"openai-gpt4"},
            cli_provider_keys={"codex"},
        )

