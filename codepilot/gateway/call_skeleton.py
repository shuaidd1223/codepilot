"""Shared call skeleton for API -> CLI gateway fallback."""

from __future__ import annotations

from typing import Callable, Optional

from codepilot.gateway.entrypoints import run_gateway_entry
from codepilot.gateway.types import GatewayMode, GatewayRequest, GatewayResponse


APIRunner = Callable[[GatewayRequest, GatewayMode], Optional[GatewayResponse]]
CLIRunner = Callable[[GatewayRequest], GatewayResponse]
FailureBuilder = Callable[..., GatewayResponse]


def make_structured_mode(
    *,
    api_formatter: Callable[[str, str], GatewayResponse],
) -> GatewayMode:
    """Create the mode contract for structured schema-constrained calls."""
    return GatewayMode(
        name="structured",
        schema_required=True,
        schema_error="call_structured requires a schema; use call_text for free-form output",
        api_formatter=api_formatter,
    )


def make_text_mode(
    *,
    api_formatter: Callable[[str, str], GatewayResponse],
) -> GatewayMode:
    """Create the mode contract for free-form text calls."""
    return GatewayMode(
        name="text",
        schema_required=False,
        schema_error="call_text is for free-form output; use call_structured with a schema",
        api_formatter=api_formatter,
    )


def run_with_fallback(
    request: GatewayRequest,
    *,
    mode: GatewayMode,
    try_api: APIRunner,
    cli_runner: CLIRunner,
    build_failure: FailureBuilder,
) -> GatewayResponse:
    """Run one gateway call through the shared API->CLI fallback skeleton."""
    return run_gateway_entry(
        request,
        mode=mode,
        try_api=try_api,
        cli_runner=cli_runner,
        build_failure=build_failure,
    )

