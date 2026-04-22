from __future__ import annotations

from codepilot.ai_gateway_call_skeleton import (
    make_structured_mode,
    make_text_mode,
    run_with_fallback,
)
from codepilot.ai_gateway_types import GatewayRequest, GatewayResponse


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
