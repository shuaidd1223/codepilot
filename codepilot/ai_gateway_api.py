"""API-provider specific behaviour for the AI gateway."""

from __future__ import annotations

import json
from typing import Optional

from codepilot.ai_gateway_types import GatewayMode, GatewayRequest, GatewayResponse


def strip_json_envelope(raw: str) -> str:
    """Best-effort recovery of a JSON body from a possibly chatty response."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start : end + 1]
    return raw.strip()


def format_api_structured(raw: str, source: str) -> GatewayResponse:
    try:
        payload = json.loads(strip_json_envelope(raw))
    except Exception as exc:  # noqa: BLE001
        return GatewayResponse(
            ok=False,
            source=source,
            error=f"api provider returned non-JSON: {exc}",
        )
    return GatewayResponse(ok=True, source=source, payload=payload)


def format_api_text(raw: str, source: str) -> GatewayResponse:
    return GatewayResponse(ok=True, source=source, text=raw.strip())


def try_api(request: GatewayRequest, mode: GatewayMode) -> Optional[GatewayResponse]:
    """Invoke configured API provider.

    Return ``None`` when no usable API path is configured so caller can continue
    with CLI fallback.
    """
    if not request.classifier_provider:
        return None

    from codepilot.ai_providers import API_PROVIDERS, _run_api_provider, resolve_api_provider

    if request.classifier_provider not in API_PROVIDERS:
        return None

    provider_ref = request.config_ref or request.project_path or None
    provider = resolve_api_provider(request.classifier_provider, provider_ref)
    if request.classifier_model:
        provider.model = request.classifier_model
    if request.api_key:
        provider.api_key = request.api_key
    if request.base_url:
        provider.base_url = request.base_url
    if provider.requires_api_key() and not provider.resolve_api_key():
        return None  # key missing -> silently skip, try CLI

    try:
        raw = _run_api_provider(provider, request.prompt)
    except Exception as exc:  # noqa: BLE001
        return GatewayResponse(
            ok=False,
            source=f"api:{request.classifier_provider}",
            error=f"api provider error: {exc}",
        )

    source = f"api:{request.classifier_provider}"
    return mode.api_formatter(raw, source)
