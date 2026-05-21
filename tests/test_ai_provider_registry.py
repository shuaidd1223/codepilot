from __future__ import annotations


def test_provider_registry_resolves_config_and_legacy_exports(tmp_path):
    from codepilot.ai_support import provider_registry
    from codepilot.ai_support import providers

    (tmp_path / "AGENTS.toml").write_text(
        """
[providers.deepseek]
api_key = "sk-config"
model = "deepseek-chat"
base_url = "https://example.test/v1"
auto_model_selection = false
max_tokens = 1234
temperature = 0.2
thinking = "enabled"
reasoning_effort = "max"
""".strip(),
        encoding="utf-8",
    )

    resolved = provider_registry.resolve_api_provider("deepseek", tmp_path)

    assert resolved.api_key == "sk-config"
    assert resolved.model == "deepseek-chat"
    assert resolved.base_url == "https://example.test/v1"
    assert resolved.max_tokens == 1234
    assert resolved.temperature == 0.2
    assert resolved.thinking == "enabled"
    assert resolved.reasoning_effort == "max"
    assert resolved.auto_model_selection is False
    assert resolved is not provider_registry.API_PROVIDERS["deepseek"]
    assert providers.APIProvider is provider_registry.APIProvider
    assert providers.API_PROVIDERS is provider_registry.API_PROVIDERS
    assert providers.resolve_api_provider("deepseek", tmp_path).api_key == "sk-config"
