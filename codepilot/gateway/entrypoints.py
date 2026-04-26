"""Call-entry routing helpers for the AI gateway facade."""

from __future__ import annotations

from typing import Callable, Optional

from codepilot.gateway.types import GatewayMode, GatewayRequest, GatewayResponse


APIRunner = Callable[[GatewayRequest, GatewayMode], Optional[GatewayResponse]]
CLIRunner = Callable[[GatewayRequest], GatewayResponse]
FailureBuilder = Callable[..., GatewayResponse]


def validate_request_mode(request: GatewayRequest, mode: GatewayMode) -> None:
    """Enforce structured/text schema contract before dispatching runners."""
    if mode.schema_required and request.schema is None:
        raise ValueError(mode.schema_error)
    if not mode.schema_required and request.schema is not None:
        raise ValueError(mode.schema_error)


def run_gateway_entry(
    request: GatewayRequest,
    *,
    mode: GatewayMode,
    try_api: APIRunner,
    cli_runner: CLIRunner,
    build_failure: FailureBuilder,
) -> GatewayResponse:
    """Single entry for API -> CLI fallback flow."""
    validate_request_mode(request, mode)

    api_result = try_api(request, mode)
    if api_result is not None and api_result.ok:
        return api_result

    cli_result = cli_runner(request)
    if cli_result.ok:
        return cli_result

    return build_failure(api_result=api_result, cli_result=cli_result)


