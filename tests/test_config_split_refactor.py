from __future__ import annotations

import pytest

from codepilot.core import config as config_mod
from codepilot.core.config import AgentsConfig, ConfigError


def test_config_parse_module_owns_normalizers_and_config_keeps_facade():
    from codepilot.core import config_parse

    assert config_parse.normalize_agent_input_language("中文") == "zh-CN"
    assert config_mod.normalize_agent_input_language is config_parse.normalize_agent_input_language
    assert config_mod.normalize_agent_output_language is config_parse.normalize_agent_output_language

    with pytest.raises(ConfigError) as excinfo:
        config_parse.normalize_preflight_dirty_worktree("archive")

    assert "automation.preflight_dirty_worktree" in str(excinfo.value)


def test_config_builder_module_preserves_agents_config_from_dict_contract():
    from codepilot.core import config_builder

    data = {
        "project": {"name": "demo", "base_branch": "main"},
        "automation": {
            "planner": "opencode",
            "fallback_cli_order": ["opencode", "codex"],
            "scheduled_agents": {
                "nightly": {
                    "agent": "codex",
                    "interval": "1h",
                    "prompt": "Run the nightly health check.",
                },
            },
        },
        "providers": {
            "openai-gpt4o": {
                "api_key": "sk-test",
                "model": "gpt-4o",
            },
        },
    }

    cfg = config_builder.build_agents_config_from_dict(AgentsConfig, data)

    assert cfg.project.name == "demo"
    assert cfg.project.base_branch == "main"
    assert cfg.automation.planner == "opencode"
    assert cfg.automation.fallback_cli_order == ["opencode", "codex"]
    assert cfg.automation.scheduled_agents["nightly"].interval_seconds == 3600
    assert cfg.providers["openai-gpt4o"].model == "gpt-4o"


def test_config_builder_parses_workflow_auto_policy_defaults_and_overrides():
    default_cfg = AgentsConfig.from_dict({"project": {"name": "demo"}})

    assert default_cfg.automation.workflow_auto_create_inspect_tasks is True
    assert default_cfg.automation.workflow_auto_import_plan_tasks is True
    assert default_cfg.automation.workflow_auto_max_steps == 3
    assert default_cfg.automation.workflow_auto_failure_threshold == 3

    overridden = AgentsConfig.from_dict(
        {
            "project": {"name": "demo"},
            "automation": {
                "workflow_auto_create_inspect_tasks": True,
                "workflow_auto_import_plan_tasks": True,
                "workflow_auto_max_steps": 4,
                "workflow_auto_failure_threshold": 2,
            },
        }
    )

    assert overridden.automation.workflow_auto_create_inspect_tasks is True
    assert overridden.automation.workflow_auto_import_plan_tasks is True
    assert overridden.automation.workflow_auto_max_steps == 4
    assert overridden.automation.workflow_auto_failure_threshold == 2
