"""Unified AI gateway routing behaviour.

Guarantees the gateway:
  * picks the API path when a provider is configured and has a key,
  * falls back to the local CLI path when no key is configured,
  * returns a uniform error shape when both paths fail,
  * preserves structured-vs-free-form distinction via the ``schema`` field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from codepilot import ai_gateway
from codepilot.ai_gateway import GatewayRequest, GatewayResponse


@dataclass
class _FakeAPIProvider:
    """Stand-in for ai_providers.APIProvider; must be a dataclass so the
    gateway's ``replace()`` call works."""

    name: str = "fake-provider"
    model: str = "fake-model"
    api_key: str = ""
    needs_key: bool = True
    raises: bool = False

    def requires_api_key(self) -> bool:
        return self.needs_key

    def resolve_api_key(self) -> str:
        return self.api_key


@pytest.fixture
def gateway_state(monkeypatch):
    """Replace the API registry and runners with fakes we can script."""
    api_calls: list[str] = []
    cli_calls: list[dict] = []

    def _fake_run_api_provider(provider, prompt):
        api_calls.append(prompt)
        if getattr(provider, "raises", False):
            raise RuntimeError("boom")
        return json.dumps({"intent": "task", "reason": "from api"})

    def _fake_run_claude_schema_prompt(prompt, schema, **_kw):
        cli_calls.append({"cli": "claude", "prompt": prompt, "schema": schema})
        return {"intent": "requirement", "reason": "from claude CLI"}

    def _fake_run_codex_schema_prompt(prompt, schema, **_kw):
        cli_calls.append({"cli": "codex", "prompt": prompt, "schema": schema})
        return {"intent": "requirement", "reason": "from codex CLI"}

    monkeypatch.setattr("codepilot.ai_providers._run_api_provider", _fake_run_api_provider)
    monkeypatch.setattr("codepilot.ai._run_claude_schema_prompt", _fake_run_claude_schema_prompt)
    monkeypatch.setattr("codepilot.ai._run_codex_schema_prompt", _fake_run_codex_schema_prompt)

    fake_registry = {}
    monkeypatch.setattr("codepilot.ai_providers.API_PROVIDERS", fake_registry)

    return {
        "api_calls": api_calls,
        "cli_calls": cli_calls,
        "registry": fake_registry,
    }


SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}}}


def test_call_structured_prefers_api_when_key_available(gateway_state):
    provider = _FakeAPIProvider(needs_key=True, api_key="sk-test")
    gateway_state["registry"]["openai"] = provider

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=SCHEMA,
            classifier_provider="openai",
            api_key="sk-test",
            planner="claude",
        )
    )

    assert resp.ok is True
    assert resp.source == "api:openai"
    assert resp.payload == {"intent": "task", "reason": "from api"}
    # CLI path must not have been touched.
    assert gateway_state["cli_calls"] == []


def test_call_structured_skips_api_when_key_missing(gateway_state):
    provider = _FakeAPIProvider(needs_key=True, api_key="")
    gateway_state["registry"]["openai"] = provider

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=SCHEMA,
            classifier_provider="openai",
            planner="claude",
        )
    )

    # API silently skipped → CLI path used.
    assert resp.ok is True
    assert resp.source == "cli:claude"
    assert gateway_state["api_calls"] == []
    assert len(gateway_state["cli_calls"]) == 1


def test_call_structured_falls_through_api_error_to_cli(gateway_state):
    provider = _FakeAPIProvider(needs_key=False, raises=True)
    gateway_state["registry"]["localcustom"] = provider

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=SCHEMA,
            classifier_provider="localcustom",
            planner="codex",
        )
    )

    assert resp.ok is True
    assert resp.source == "cli:codex"
    assert gateway_state["cli_calls"][0]["cli"] == "codex"


def test_call_structured_reports_combined_error_when_both_fail(gateway_state, monkeypatch):
    provider = _FakeAPIProvider(needs_key=False, raises=True)
    gateway_state["registry"]["localcustom"] = provider

    def _cli_raises(*_a, **_kw):
        raise RuntimeError("cli down")

    monkeypatch.setattr("codepilot.ai._run_codex_schema_prompt", _cli_raises)

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=SCHEMA,
            classifier_provider="localcustom",
            planner="codex",
        )
    )

    assert resp.ok is False
    assert "api provider error" in resp.error
    assert "codex CLI error" in resp.error


def test_call_text_requires_no_schema():
    with pytest.raises(ValueError):
        ai_gateway.call_text(
            GatewayRequest(prompt="hi", schema={"anything": 1})
        )


def test_call_structured_requires_schema():
    with pytest.raises(ValueError):
        ai_gateway.call_structured(GatewayRequest(prompt="hi"))
