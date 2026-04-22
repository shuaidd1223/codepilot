from __future__ import annotations

import pytest

from codepilot import ai_gateway
from codepilot.ai_gateway import GatewayCallOptions, GatewayRequest, GatewayResponse
from tests.ai_gateway_testkit import STRUCTURED_SCHEMA


def test_call_structured_prompt_builds_request_with_shared_options(monkeypatch):
    captured = {}

    def _fake_call_structured(request):
        captured["request"] = request
        return GatewayResponse(ok=True, source="api:test", payload={"intent": "task"})

    monkeypatch.setattr(ai_gateway, "call_structured", _fake_call_structured)

    resp = ai_gateway.call_structured_prompt(
        prompt="classify me",
        schema=STRUCTURED_SCHEMA,
        options=GatewayCallOptions(
            classifier_provider="openai",
            classifier_model="gpt-x",
            api_key="sk-1",
            base_url="https://models.example.invalid/v1",
            project_path="D:/demo/project",
            config_ref="D:/demo/config/AGENTS.toml",
            planner="claude",
            timeout=33,
        ),
    )

    assert resp.ok is True
    req = captured["request"]
    assert req.prompt == "classify me"
    assert req.schema == STRUCTURED_SCHEMA
    assert req.classifier_provider == "openai"
    assert req.classifier_model == "gpt-x"
    assert req.api_key == "sk-1"
    assert req.base_url == "https://models.example.invalid/v1"
    assert req.project_path == "D:/demo/project"
    assert req.config_ref == "D:/demo/config/AGENTS.toml"
    assert req.planner == "claude"
    assert req.timeout == 33


def test_call_text_prompt_builds_request_with_shared_options(monkeypatch):
    captured = {}

    def _fake_call_text(request):
        captured["request"] = request
        return GatewayResponse(ok=True, source="cli:claude", text="ok")

    monkeypatch.setattr(ai_gateway, "call_text", _fake_call_text)

    resp = ai_gateway.call_text_prompt(
        prompt="answer me",
        options=GatewayCallOptions(
            classifier_provider="openai",
            classifier_model="gpt-y",
            api_key="sk-2",
            base_url="https://models.example.invalid/v2",
            project_path="D:/demo/project",
            config_ref="D:/demo/config/AGENTS.toml",
            planner="claude",
            timeout=44,
        ),
    )

    assert resp.ok is True
    req = captured["request"]
    assert req.prompt == "answer me"
    assert req.schema is None
    assert req.classifier_provider == "openai"
    assert req.classifier_model == "gpt-y"
    assert req.api_key == "sk-2"
    assert req.base_url == "https://models.example.invalid/v2"
    assert req.project_path == "D:/demo/project"
    assert req.config_ref == "D:/demo/config/AGENTS.toml"
    assert req.planner == "claude"
    assert req.timeout == 44


def test_call_text_requires_no_schema():
    with pytest.raises(ValueError):
        ai_gateway.call_text(
            GatewayRequest(prompt="hi", schema={"anything": 1})
        )


def test_call_structured_requires_schema():
    with pytest.raises(ValueError):
        ai_gateway.call_structured(GatewayRequest(prompt="hi"))
