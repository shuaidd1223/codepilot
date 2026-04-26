"""Error aggregation helpers for AI gateway fallback results."""

from __future__ import annotations

from typing import Optional

from codepilot.gateway.types import GatewayResponse


def aggregate_errors(*errors: str) -> str:
    """Join non-empty error fragments with a stable separator."""
    return "; ".join(err for err in errors if err)


def build_combined_failure(
    *,
    api_result: Optional[GatewayResponse],
    cli_result: GatewayResponse,
) -> GatewayResponse:
    """Return one normalized failure response after API->CLI fallback."""
    api_err = api_result.error if api_result is not None else ""
    return GatewayResponse(
        ok=False,
        source=cli_result.source,
        error=aggregate_errors(api_err, cli_result.error),
    )


