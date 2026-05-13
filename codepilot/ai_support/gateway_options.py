"""Shared gateway option resolution for classifier-facing flows."""

from __future__ import annotations

from typing import Optional

from codepilot.gateway.types import GatewayCallOptions


def _resolve_gateway_options(
    *,
    gateway_options: Optional[GatewayCallOptions],
    classifier_provider: str,
    classifier_model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    project_path: str,
    config_ref: str,
    planner: str,
    timeout: int,
) -> GatewayCallOptions:
    """Merge explicit kwargs with an optional shared gateway context object."""
    shared = gateway_options
    effective_timeout = timeout
    if shared is not None and shared.timeout:
        effective_timeout = int(shared.timeout)
    return GatewayCallOptions(
        classifier_provider=(shared.classifier_provider if shared else "") or classifier_provider,
        classifier_model=(shared.classifier_model if shared else "") or classifier_model,
        api_key=shared.api_key if shared and shared.api_key is not None else api_key,
        base_url=shared.base_url if shared and shared.base_url is not None else base_url,
        project_path=(shared.project_path if shared else "") or project_path,
        config_ref=(shared.config_ref if shared else "") or config_ref,
        planner=planner,
        timeout=effective_timeout,
    )
