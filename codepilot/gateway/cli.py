"""CLI-runner specific behaviour for the AI gateway."""

from __future__ import annotations

from codepilot.gateway.execute import (
    execute_structured_cli_call,
    execute_text_cli_candidate,
)
from codepilot.gateway.resolution import (
    resolve_structured_cli_call,
    resolve_text_cli_candidates,
)
from codepilot.gateway.types import GatewayRequest, GatewayResponse


def try_cli_structured(request: GatewayRequest) -> GatewayResponse:
    """Invoke local CLI planner with a JSON schema contract."""
    resolved = resolve_structured_cli_call(request)

    try:
        payload = execute_structured_cli_call(request, resolved)
    except Exception as exc:  # noqa: BLE001
        return GatewayResponse(
            ok=False,
            source=resolved.source,
            error=f"{resolved.cli_name} CLI error: {exc}",
        )
    return GatewayResponse(ok=True, source=resolved.source, payload=payload)


def try_cli_text(request: GatewayRequest) -> GatewayResponse:
    """Invoke local CLI planner expecting free-form text output."""
    candidates, last_error = resolve_text_cli_candidates(request)
    for candidate in candidates:
        ok, text, error = execute_text_cli_candidate(request, candidate)
        if ok:
            return GatewayResponse(ok=True, source=candidate.source, text=text)
        if error:
            last_error = error

    return GatewayResponse(
        ok=False,
        source="cli:none",
        error=last_error or "no local CLI available",
    )

