"""Unified AI gateway routing behaviour.

Guarantees the gateway:
  * picks the API path when a provider is configured and has a key,
  * falls back to the local CLI path when no key is configured,
  * returns a uniform error shape when both paths fail.
"""

from __future__ import annotations

import pytest

from codepilot import ai_gateway
from codepilot.ai_gateway import GatewayRequest, GatewayResponse
from tests.ai_gateway_testkit import STRUCTURED_SCHEMA, FakeAPIProvider, gateway_state


@pytest.mark.parametrize(
    ("provider_key", "provider_kwargs", "request_kwargs", "expected"),
    [
        (
            "openai",
            {"needs_key": True, "api_key": "sk-test"},
            {"classifier_provider": "openai", "api_key": "sk-test", "planner": "claude"},
            {"source": "api:openai", "api_calls": 1, "cli_calls": 0, "cli_name": ""},
        ),
        (
            "openai",
            {"needs_key": True, "api_key": ""},
            {"classifier_provider": "openai", "planner": "claude"},
            {"source": "cli:claude", "api_calls": 0, "cli_calls": 1, "cli_name": "claude"},
        ),
        (
            "localcustom",
            {"needs_key": False, "raises": True},
            {"classifier_provider": "localcustom", "planner": "codex"},
            {"source": "cli:codex", "api_calls": 1, "cli_calls": 1, "cli_name": "codex"},
        ),
    ],
    ids=["api-success", "missing-key-cli-fallback", "api-error-cli-fallback"],
)
def test_call_structured_route_matrix(gateway_state, provider_key, provider_kwargs, request_kwargs, expected):
    provider = FakeAPIProvider(**provider_kwargs)
    gateway_state["registry"][provider_key] = provider

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            **request_kwargs,
        )
    )

    assert resp.ok is True
    assert resp.source == expected["source"]
    assert len(gateway_state["api_calls"]) == expected["api_calls"]
    assert len(gateway_state["cli_calls"]) == expected["cli_calls"]
    if expected["cli_name"]:
        assert gateway_state["cli_calls"][0]["cli"] == expected["cli_name"]
    if expected["source"].startswith("api:"):
        assert resp.payload == {"intent": "task", "reason": "from api"}


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


def test_call_structured_passes_config_ref_to_cli(gateway_state):
    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            planner="claude",
            project_path="C:/project",
            config_ref="C:/config-root/AGENTS.toml",
        )
    )

    assert resp.ok is True
    assert resp.source == "cli:claude"
    assert gateway_state["cli_calls"][0]["kwargs"]["project_path"] == "C:/project"
    assert gateway_state["cli_calls"][0]["kwargs"]["config_ref"] == "C:/config-root/AGENTS.toml"


def test_call_structured_reports_combined_error_when_both_fail(gateway_state, monkeypatch):
    provider = FakeAPIProvider(needs_key=False, raises=True)
    gateway_state["registry"]["localcustom"] = provider

    def _cli_raises(*_a, **_kw):
        raise RuntimeError("cli down")

    monkeypatch.setattr("codepilot.ai._run_codex_schema_prompt", _cli_raises)

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            classifier_provider="localcustom",
            planner="codex",
        )
    )

    assert resp.ok is False
    assert "api provider error" in resp.error
    assert "codex CLI error" in resp.error


def test_call_text_reports_combined_error_when_both_fail(monkeypatch):
    monkeypatch.setattr(
        ai_gateway,
        "_try_api",
        lambda _request, _mode: GatewayResponse(ok=False, source="api:test", error="api down"),
    )
    monkeypatch.setattr(
        ai_gateway,
        "_try_cli_text",
        lambda _request: GatewayResponse(ok=False, source="cli:none", error="cli down"),
    )

    resp = ai_gateway.call_text(GatewayRequest(prompt="hi"))

    assert resp.ok is False
    assert resp.source == "cli:none"
    assert resp.error == "api down; cli down"


def test_call_text_prefers_api_when_key_available(gateway_state):
    provider = FakeAPIProvider(needs_key=True, api_key="sk-test")
    gateway_state["registry"]["openai"] = provider

    resp = ai_gateway.call_text(
        GatewayRequest(
            prompt="hi",
            classifier_provider="openai",
            api_key="sk-test",
            planner="claude",
        )
    )

    assert resp.ok is True
    assert resp.source == "api:openai"
    assert resp.text == '{"intent": "task", "reason": "from api"}'
    assert gateway_state["cli_calls"] == []
