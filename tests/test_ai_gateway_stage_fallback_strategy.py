from __future__ import annotations

import pytest

from codepilot import ai_gateway
from codepilot.ai_gateway_call_skeleton import (
    make_structured_mode,
    make_text_mode,
    run_with_fallback,
)
from codepilot.ai_gateway_entrypoints import run_gateway_entry, validate_request_mode
from codepilot.ai_gateway_errors import aggregate_errors, build_combined_failure
from codepilot.ai_gateway_types import GatewayMode, GatewayRequest, GatewayResponse
from tests.ai_gateway_assertions import assert_failure_response
from tests.ai_gateway_testkit import FakeAPIProvider, STRUCTURED_SCHEMA, gateway_state


def _structured_mode() -> GatewayMode:
    return GatewayMode(
        name="structured",
        schema_required=True,
        schema_error="schema required",
        api_formatter=lambda raw, source: GatewayResponse(ok=True, source=source, payload={"raw": raw}),
    )


def _text_mode() -> GatewayMode:
    return GatewayMode(
        name="text",
        schema_required=False,
        schema_error="schema forbidden",
        api_formatter=lambda raw, source: GatewayResponse(ok=True, source=source, text=raw),
    )


@pytest.mark.parametrize(
    ("gateway_request", "mode_factory", "error"),
    [
        (GatewayRequest(prompt="x"), _structured_mode, "schema required"),
        (GatewayRequest(prompt="x", schema={"type": "object"}), _text_mode, "schema forbidden"),
    ],
    ids=["structured-requires-schema", "text-rejects-schema"],
)
def test_validate_request_mode_matrix(gateway_request, mode_factory, error):
    with pytest.raises(ValueError, match=error):
        validate_request_mode(gateway_request, mode_factory())


def test_run_gateway_entry_prefers_api_success():
    req = GatewayRequest(prompt="x", schema={"type": "object"})

    resp = run_gateway_entry(
        req,
        mode=_structured_mode(),
        try_api=lambda *_: GatewayResponse(ok=True, source="api:test", payload={"ok": True}),
        cli_runner=lambda *_: GatewayResponse(ok=True, source="cli:codex", payload={"ok": False}),
        build_failure=lambda **_: GatewayResponse(ok=False, source="cli:none", error="unexpected"),
    )

    assert resp.ok is True
    assert resp.source == "api:test"
    assert resp.payload == {"ok": True}


def test_run_gateway_entry_combines_failure_when_api_and_cli_fail():
    req = GatewayRequest(prompt="x", schema={"type": "object"})

    resp = run_gateway_entry(
        req,
        mode=_structured_mode(),
        try_api=lambda *_: GatewayResponse(ok=False, source="api:test", error="api down"),
        cli_runner=lambda *_: GatewayResponse(ok=False, source="cli:codex", error="cli down"),
        build_failure=build_combined_failure,
    )

    assert_failure_response(
        resp,
        source="cli:codex",
        error_exact="api down; cli down",
    )


def test_aggregate_errors_skips_empty_fragments():
    assert aggregate_errors("", "api down", "", "cli down") == "api down; cli down"


def test_build_combined_failure_without_api_error_uses_cli_error():
    resp = build_combined_failure(
        api_result=None,
        cli_result=GatewayResponse(ok=False, source="cli:none", error="no local CLI available"),
    )

    assert_failure_response(
        resp,
        source="cli:none",
        error_exact="no local CLI available",
    )


def test_make_structured_mode_contract():
    mode = make_structured_mode(
        api_formatter=lambda raw, source: GatewayResponse(ok=True, source=source, payload={"raw": raw}),
    )

    assert mode.name == "structured"
    assert mode.schema_required is True
    assert "requires a schema" in mode.schema_error


def test_make_text_mode_contract():
    mode = make_text_mode(
        api_formatter=lambda raw, source: GatewayResponse(ok=True, source=source, text=raw),
    )

    assert mode.name == "text"
    assert mode.schema_required is False
    assert "free-form output" in mode.schema_error


def test_run_with_fallback_delegates_to_entrypoint(monkeypatch):
    captured = {}

    def _fake_entry(request, *, mode, try_api, cli_runner, build_failure):
        captured["request"] = request
        captured["mode"] = mode
        captured["try_api"] = try_api
        captured["cli_runner"] = cli_runner
        captured["build_failure"] = build_failure
        return GatewayResponse(ok=True, source="api:test", payload={"ok": True})

    monkeypatch.setattr("codepilot.ai_gateway_call_skeleton.run_gateway_entry", _fake_entry)

    request = GatewayRequest(prompt="hello", schema={"type": "object"})
    mode = make_structured_mode(
        api_formatter=lambda raw, source: GatewayResponse(ok=True, source=source, payload={"raw": raw}),
    )

    def _try_api(_request, _mode):
        return GatewayResponse(ok=False, source="api:test", error="nope")

    def _cli_runner(_request):
        return GatewayResponse(ok=True, source="cli:codex", payload={"ok": True})

    def _failure_builder(**_kwargs):
        return GatewayResponse(ok=False, source="cli:none", error="bad")

    resp = run_with_fallback(
        request,
        mode=mode,
        try_api=_try_api,
        cli_runner=_cli_runner,
        build_failure=_failure_builder,
    )

    assert resp.ok is True
    assert captured["request"] is request
    assert captured["mode"] is mode
    assert captured["try_api"] is _try_api
    assert captured["cli_runner"] is _cli_runner
    assert captured["build_failure"] is _failure_builder


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

    assert_failure_response(
        resp,
        source="cli:codex",
        error_contains=("api provider error", "codex CLI error"),
    )


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

    assert_failure_response(
        resp,
        source="cli:none",
        error_exact="api down; cli down",
    )
