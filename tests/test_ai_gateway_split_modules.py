from __future__ import annotations

import pytest

from codepilot.ai_gateway_entrypoints import run_gateway_entry, validate_request_mode
from codepilot.ai_gateway_errors import aggregate_errors, build_combined_failure
from codepilot.ai_gateway_types import GatewayMode, GatewayRequest, GatewayResponse


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


def test_validate_request_mode_for_structured_requires_schema():
    with pytest.raises(ValueError, match="schema required"):
        validate_request_mode(GatewayRequest(prompt="x"), _structured_mode())


def test_validate_request_mode_for_text_rejects_schema():
    with pytest.raises(ValueError, match="schema forbidden"):
        validate_request_mode(GatewayRequest(prompt="x", schema={"type": "object"}), _text_mode())


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

    assert resp.ok is False
    assert resp.source == "cli:codex"
    assert resp.error == "api down; cli down"


def test_aggregate_errors_skips_empty_fragments():
    assert aggregate_errors("", "api down", "", "cli down") == "api down; cli down"


def test_build_combined_failure_without_api_error_uses_cli_error():
    resp = build_combined_failure(
        api_result=None,
        cli_result=GatewayResponse(ok=False, source="cli:none", error="no local CLI available"),
    )

    assert resp.ok is False
    assert resp.source == "cli:none"
    assert resp.error == "no local CLI available"

