"""API-provider specific behaviour for the AI gateway."""

from __future__ import annotations

import json
from typing import Optional

from codepilot.gateway.execute import execute_api_prompt
from codepilot.gateway.resolution import resolve_api_call
from codepilot.gateway.types import GatewayMode, GatewayRequest, GatewayResponse


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
    resolved = resolve_api_call(request)
    if resolved is None:
        return None

    try:
        raw = execute_api_prompt(resolved.provider, request.prompt)
    except Exception as exc:  # noqa: BLE001
        return GatewayResponse(
            ok=False,
            source=resolved.source,
            error=f"api provider error: {exc}",
        )

    return mode.api_formatter(raw, resolved.source)

