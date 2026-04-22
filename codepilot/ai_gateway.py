"""Unified facade for AI calls.

Public API stays in this module (`call_structured`, `call_text`, and request
builders), while provider-specific execution now lives in focused modules:

* `ai_gateway_api`: configured API-provider path + JSON normalization.
* `ai_gateway_cli`: local CLI runners for structured/text modes.

This keeps callsites stable and makes responsibilities explicit:
the facade owns request building and fallback orchestration only.
"""

from __future__ import annotations

from typing import Optional

from codepilot.ai_gateway_entrypoints import run_gateway_entry
from codepilot.ai_gateway_errors import build_combined_failure
from codepilot.ai_gateway_api import (
    format_api_structured as _api_format_api_structured,
    format_api_text as _api_format_api_text,
    strip_json_envelope as _api_strip_json_envelope,
    try_api as _api_try_api,
)
from codepilot.ai_gateway_cli import (
    try_cli_structured as _cli_try_structured,
    try_cli_text as _cli_try_text,
)
from codepilot.ai_gateway_types import (
    GatewayCallOptions,
    GatewayMode as _GatewayMode,
    GatewayRequest,
    GatewayResponse,
)


def _strip_json_envelope(raw: str) -> str:
    """Compatibility shim for existing tests and private monkeypatching."""
    return _api_strip_json_envelope(raw)


def _format_api_structured(raw: str, source: str) -> GatewayResponse:
    """Compatibility shim for existing tests and private monkeypatching."""
    return _api_format_api_structured(raw, source)


def _format_api_text(raw: str, source: str) -> GatewayResponse:
    """Compatibility shim for existing tests and private monkeypatching."""
    return _api_format_api_text(raw, source)


def _try_api(request: GatewayRequest, mode: _GatewayMode) -> Optional[GatewayResponse]:
    """Compatibility shim for existing tests and private monkeypatching."""
    return _api_try_api(request, mode)


def _try_cli_structured(request: GatewayRequest) -> GatewayResponse:
    """Compatibility shim for existing tests and private monkeypatching."""
    return _cli_try_structured(request)


def _try_cli_text(request: GatewayRequest) -> GatewayResponse:
    """Compatibility shim for existing tests and private monkeypatching."""
    return _cli_try_text(request)


_STRUCTURED_MODE = _GatewayMode(
    name="structured",
    schema_required=True,
    schema_error="call_structured requires a schema; use call_text for free-form output",
    api_formatter=_format_api_structured,
)
_TEXT_MODE = _GatewayMode(
    name="text",
    schema_required=False,
    schema_error="call_text is for free-form output; use call_structured with a schema",
    api_formatter=_format_api_text,
)


def _build_request(
    prompt: str,
    *,
    schema: Optional[dict],
    options: Optional[GatewayCallOptions],
) -> GatewayRequest:
    opts = options or GatewayCallOptions()
    return GatewayRequest(
        prompt=prompt,
        schema=schema,
        classifier_provider=opts.classifier_provider,
        classifier_model=opts.classifier_model,
        api_key=opts.api_key,
        base_url=opts.base_url,
        project_path=opts.project_path,
        config_ref=opts.config_ref,
        planner=opts.planner,
        timeout=opts.timeout,
    )


def call_structured_prompt(
    *,
    prompt: str,
    schema: dict,
    options: Optional[GatewayCallOptions] = None,
) -> GatewayResponse:
    """Build a structured request from shared options then run gateway."""
    return call_structured(_build_request(prompt, schema=schema, options=options))


def call_text_prompt(
    *,
    prompt: str,
    options: Optional[GatewayCallOptions] = None,
) -> GatewayResponse:
    """Build a text request from shared options then run gateway."""
    return call_text(_build_request(prompt, schema=None, options=options))


def call_structured(request: GatewayRequest) -> GatewayResponse:
    """Run a prompt expecting a JSON payload matching ``request.schema``."""
    return _call_with_fallback(request, mode=_STRUCTURED_MODE, cli_runner=_try_cli_structured)


def call_text(request: GatewayRequest) -> GatewayResponse:
    """Run a prompt expecting a free-form textual answer."""
    return _call_with_fallback(request, mode=_TEXT_MODE, cli_runner=_try_cli_text)


def _call_with_fallback(
    request: GatewayRequest,
    *,
    mode: _GatewayMode,
    cli_runner,
) -> GatewayResponse:
    """Single fallback decision entry for API -> CLI routing."""
    return run_gateway_entry(
        request,
        mode=mode,
        try_api=_try_api,
        cli_runner=cli_runner,
        build_failure=build_combined_failure,
    )
