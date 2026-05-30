from __future__ import annotations

import pytest

from codepilot.gateway import service as ai_gateway
from codepilot.gateway.service import GatewayRequest, GatewayResponse
from codepilot.gateway.prompt_build import (
    build_request,
    call_structured_prompt,
    call_text_prompt,
)
from tests.ai_gateway_assertions import make_gateway_options, assert_request_matches_options
from tests.ai_gateway_testkit import STRUCTURED_SCHEMA


def test_build_request_copies_shared_options():
    options = make_gateway_options()
    req = build_request(
        "hello",
        schema=STRUCTURED_SCHEMA,
        options=options,
    )

    assert_request_matches_options(
        req,
        prompt="hello",
        schema=STRUCTURED_SCHEMA,
        options=options,
    )


def test_call_structured_prompt_builds_request_then_dispatches():
    captured = {}
    options = make_gateway_options()

    def _fake_call_structured(request):
        captured["request"] = request
        return GatewayResponse(ok=True, source="api:test", payload={"ok": True})

    resp = call_structured_prompt(
        prompt="classify this",
        schema=STRUCTURED_SCHEMA,
        options=options,
        call_structured_fn=_fake_call_structured,
    )

    assert resp.ok is True
    assert_request_matches_options(
        captured["request"],
        prompt="classify this",
        schema=STRUCTURED_SCHEMA,
        options=options,
    )


def test_call_text_prompt_builds_request_then_dispatches():
    captured = {}
    options = make_gateway_options()

    def _fake_call_text(request):
        captured["request"] = request
        return GatewayResponse(ok=True, source="cli:claude", text="ok")

    resp = call_text_prompt(
        prompt="answer this",
        options=options,
        call_text_fn=_fake_call_text,
    )

    assert resp.ok is True
    assert_request_matches_options(
        captured["request"],
        prompt="answer this",
        schema=None,
        options=options,
    )


def test_facade_call_structured_prompt_builds_request_with_shared_options(monkeypatch):
    captured = {}
    options = make_gateway_options(timeout=33)

    def _fake_call_structured(request):
        captured["request"] = request
        return GatewayResponse(ok=True, source="api:test", payload={"intent": "task"})

    monkeypatch.setattr(ai_gateway, "call_structured", _fake_call_structured)

    resp = ai_gateway.call_structured_prompt(
        prompt="classify me",
        schema=STRUCTURED_SCHEMA,
        options=options,
    )

    assert resp.ok is True
    assert_request_matches_options(
        captured["request"],
        prompt="classify me",
        schema=STRUCTURED_SCHEMA,
        options=options,
    )


def test_facade_call_text_prompt_builds_request_with_shared_options(monkeypatch):
    captured = {}
    options = make_gateway_options(
        llm_model="gpt-y",
        api_key="sk-2",
        base_url="https://models.example.invalid/v2",
        timeout=44,
    )

    def _fake_call_text(request):
        captured["request"] = request
        return GatewayResponse(ok=True, source="cli:claude", text="ok")

    monkeypatch.setattr(ai_gateway, "call_text", _fake_call_text)

    resp = ai_gateway.call_text_prompt(
        prompt="answer me",
        options=options,
    )

    assert resp.ok is True
    assert_request_matches_options(
        captured["request"],
        prompt="answer me",
        schema=None,
        options=options,
    )


def test_call_text_requires_no_schema():
    with pytest.raises(ValueError):
        ai_gateway.call_text(
            GatewayRequest(prompt="hi", schema={"anything": 1})
        )


def test_call_structured_requires_schema():
    with pytest.raises(ValueError):
        ai_gateway.call_structured(GatewayRequest(prompt="hi"))

