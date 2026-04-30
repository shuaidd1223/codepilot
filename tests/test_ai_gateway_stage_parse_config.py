from __future__ import annotations

from codepilot.gateway import service as ai_gateway
from codepilot.gateway.service import GatewayRequest
from codepilot.gateway.resolution import (
    resolve_api_call,
    resolve_structured_cli_call,
    resolve_text_cli_candidates,
)
from tests.ai_gateway_testkit import (
    FakeAPIProvider,
    FakeCLIProvider,
    STRUCTURED_SCHEMA,
    gateway_state,
)


def test_resolve_api_call_applies_overrides_and_prefers_config_ref(monkeypatch):
    provider = FakeAPIProvider(model="from-registry", api_key="registry-key")
    captured = {}

    monkeypatch.setattr("codepilot.ai_support.providers.API_PROVIDERS", {"openai": object()})

    def _fake_resolve(provider_key, provider_ref):
        captured["provider_key"] = provider_key
        captured["provider_ref"] = provider_ref
        return provider

    monkeypatch.setattr("codepilot.ai_support.providers.resolve_api_provider", _fake_resolve)

    resolved = resolve_api_call(
        GatewayRequest(
            prompt="hi",
            classifier_provider="openai",
            classifier_model="custom-model",
            api_key="sk-custom",
            base_url="https://models.example.invalid/v1",
            project_path="D:/project",
            config_ref="D:/config/AGENTS.toml",
        )
    )

    assert resolved is not None
    assert resolved.source == "api:openai"
    assert captured["provider_key"] == "openai"
    assert captured["provider_ref"] == "D:/config/AGENTS.toml"
    assert provider.model == "custom-model"
    assert provider.api_key == "sk-custom"
    assert provider.base_url == "https://models.example.invalid/v1"


def test_resolve_api_call_returns_none_when_required_key_missing(monkeypatch):
    marks: list[dict[str, object]] = []
    provider = FakeAPIProvider(needs_key=True, api_key="")
    monkeypatch.setattr("codepilot.ai_support.providers.API_PROVIDERS", {"openai": object()})
    monkeypatch.setattr("codepilot.ai_support.providers.resolve_api_provider", lambda *_: provider)
    monkeypatch.setattr(
        "codepilot.gateway.resolution.mark_provider_unavailable",
        lambda provider_key, provider_obj, reason, **kwargs: marks.append(
            {
                "provider_key": provider_key,
                "provider": provider_obj,
                "reason": reason,
                "source": kwargs.get("source"),
            }
        ),
    )

    resolved = resolve_api_call(
        GatewayRequest(
            prompt="hi",
            classifier_provider="openai",
        )
    )

    assert resolved is None
    assert marks == [
        {
            "provider_key": "openai",
            "provider": provider,
            "reason": "missing api key",
            "source": "gateway",
        }
    ]


def test_resolve_structured_cli_call_preserves_claude_variant():
    resolved = resolve_structured_cli_call(
        GatewayRequest(prompt="hi", planner="claude-sonnet", schema={"type": "object"})
    )

    assert resolved.cli_name == "claude"
    assert resolved.planner == "claude-sonnet"
    assert resolved.source == "cli:claude"


def test_resolve_text_cli_candidates_builds_codex_command_with_project_path(monkeypatch):
    def _fake_resolve_cli_provider(cli_name, _provider_ref):
        if cli_name == "claude":
            return FakeCLIProvider(exe="")
        return FakeCLIProvider(exe="C:/bin/codex.exe")

    monkeypatch.setattr("codepilot.ai_support.providers.resolve_cli_provider", _fake_resolve_cli_provider)

    candidates, last_error = resolve_text_cli_candidates(
        GatewayRequest(
            prompt="hi",
            planner="codex",
            project_path="D:/demo/project",
        )
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.cli_name == "codex"
    assert candidate.source == "cli:codex"
    assert candidate.cmd[:3] == ["C:/bin/codex.exe", "-C", "D:/demo/project"]
    assert "claude CLI not installed" in last_error


def test_call_structured_applies_api_overrides(gateway_state):
    provider = FakeAPIProvider(needs_key=True, api_key="registry-key")
    gateway_state["registry"]["openai"] = provider

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            classifier_provider="openai",
            classifier_model="custom-model",
            api_key="sk-custom",
            base_url="https://models.example.invalid/v1",
            planner="claude",
        )
    )

    assert resp.ok is True
    used = gateway_state["api_providers"][0]
    assert used.model == "custom-model"
    assert used.api_key == "sk-custom"
    assert used.base_url == "https://models.example.invalid/v1"


def test_call_structured_uses_provider_model_from_project_config(gateway_state, tmp_path):
    provider = FakeAPIProvider(needs_key=True, api_key="")
    gateway_state["registry"]["openai"] = provider
    (tmp_path / "AGENTS.toml").write_text(
        "\n".join(
            [
                "[providers.openai]",
                'api_key = "sk-from-config"',
                'model = "custom-config-model"',
                'base_url = "https://models.example.invalid/v1"',
            ]
        ),
        encoding="utf-8",
    )

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            classifier_provider="openai",
            project_path=str(tmp_path),
            planner="claude",
        )
    )

    assert resp.ok is True
    used = gateway_state["api_providers"][0]
    assert used.model == "custom-config-model"
    assert used.api_key == "sk-from-config"
    assert used.base_url == "https://models.example.invalid/v1"

