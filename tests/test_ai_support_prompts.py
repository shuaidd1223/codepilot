from __future__ import annotations

from codepilot.ai_support import prompts, providers


def test_agent_config_resolves_api_agents_from_provider_registry(monkeypatch):
    monkeypatch.setattr(providers, "API_PROVIDERS", {"unit-api": object()})

    config = prompts.AgentConfig()
    config.builder = "unit-api"
    config.reviewer = "unit-api"

    assert config.resolve_builder() == ("api", "unit-api")
    assert config.resolve_reviewer() == ("api", "unit-api")
