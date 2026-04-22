from __future__ import annotations

from codepilot.ai_gateway_prompt_build import (
    build_request,
    call_structured_prompt,
    call_text_prompt,
)
from codepilot.ai_gateway_types import GatewayCallOptions, GatewayResponse


def _shared_options() -> GatewayCallOptions:
    return GatewayCallOptions(
        classifier_provider="openai",
        classifier_model="gpt-x",
        api_key="sk-1",
        base_url="https://models.example.invalid/v1",
        project_path="D:/demo/project",
        config_ref="D:/demo/config/AGENTS.toml",
        planner="claude",
        timeout=42,
    )


def test_build_request_copies_shared_options():
    req = build_request(
        "hello",
        schema={"type": "object"},
        options=_shared_options(),
    )

    assert req.prompt == "hello"
    assert req.schema == {"type": "object"}
    assert req.classifier_provider == "openai"
    assert req.classifier_model == "gpt-x"
    assert req.api_key == "sk-1"
    assert req.base_url == "https://models.example.invalid/v1"
    assert req.project_path == "D:/demo/project"
    assert req.config_ref == "D:/demo/config/AGENTS.toml"
    assert req.planner == "claude"
    assert req.timeout == 42


def test_call_structured_prompt_builds_request_then_dispatches():
    captured = {}

    def _fake_call_structured(request):
        captured["request"] = request
        return GatewayResponse(ok=True, source="api:test", payload={"ok": True})

    resp = call_structured_prompt(
        prompt="classify this",
        schema={"type": "object"},
        options=_shared_options(),
        call_structured_fn=_fake_call_structured,
    )

    assert resp.ok is True
    assert captured["request"].prompt == "classify this"
    assert captured["request"].schema == {"type": "object"}


def test_call_text_prompt_builds_request_then_dispatches():
    captured = {}

    def _fake_call_text(request):
        captured["request"] = request
        return GatewayResponse(ok=True, source="cli:claude", text="ok")

    resp = call_text_prompt(
        prompt="answer this",
        options=_shared_options(),
        call_text_fn=_fake_call_text,
    )

    assert resp.ok is True
    assert captured["request"].prompt == "answer this"
    assert captured["request"].schema is None
