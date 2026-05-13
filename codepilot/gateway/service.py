"""Unified facade for AI calls.

Public API stays in this module (`call_structured`, `call_text`, and prompt
entrypoints), while provider-specific execution now lives in focused modules:

* `ai_gateway_api`: configured API-provider path + JSON normalization.
* `ai_gateway_cli`: local CLI runners for structured/text modes.
* `ai_gateway_prompt_build`: request/prompt construction helpers.
* `ai_gateway_call_skeleton`: shared API->CLI fallback skeleton assembly.

This keeps callsites stable while shrinking facade responsibilities to wiring.
"""

from __future__ import annotations

from typing import Optional

from codepilot.gateway.call_skeleton import (
    make_structured_mode as _skeleton_make_structured_mode,
    make_text_mode as _skeleton_make_text_mode,
    run_with_fallback as _skeleton_run_with_fallback,
)
from codepilot.gateway.errors import build_combined_failure
from codepilot.gateway.api import (
    format_api_structured as _api_format_api_structured,
    format_api_text as _api_format_api_text,
    strip_json_envelope as _api_strip_json_envelope,
    try_api as _api_try_api,
)
from codepilot.gateway.cli import (
    try_cli_structured as _cli_try_structured,
    try_cli_text as _cli_try_text,
)
from codepilot.gateway.prompt_build import (
    build_request as _prompt_build_request,
    call_structured_prompt as _prompt_call_structured_prompt,
    call_text_prompt as _prompt_call_text_prompt,
)
from codepilot.gateway.types import (
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


_STRUCTURED_MODE = _skeleton_make_structured_mode(api_formatter=_format_api_structured)
_TEXT_MODE = _skeleton_make_text_mode(api_formatter=_format_api_text)


def _build_request(
    prompt: str,
    *,
    schema: Optional[dict],
    options: Optional[GatewayCallOptions],
) -> GatewayRequest:
    """Compatibility shim for private imports and tests."""
    return _prompt_build_request(prompt, schema=schema, options=options)


def call_structured_prompt(
    *,
    prompt: str,
    schema: dict,
    options: Optional[GatewayCallOptions] = None,
) -> GatewayResponse:
    """Build a structured request from shared options then run gateway."""
    return _prompt_call_structured_prompt(
        prompt=prompt,
        schema=schema,
        options=options,
        call_structured_fn=call_structured,
    )


def call_text_prompt(
    *,
    prompt: str,
    options: Optional[GatewayCallOptions] = None,
) -> GatewayResponse:
    """Build a text request from shared options then run gateway."""
    return _prompt_call_text_prompt(
        prompt=prompt,
        options=options,
        call_text_fn=call_text,
    )


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
    return _skeleton_run_with_fallback(
        request=request,
        mode=mode,
        try_api=_try_api,
        cli_runner=cli_runner,
        build_failure=build_combined_failure,
    )

