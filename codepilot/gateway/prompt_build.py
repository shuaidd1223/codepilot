"""Prompt/request construction helpers for the AI gateway facade."""

from __future__ import annotations

from typing import Callable, Optional

from codepilot.gateway.types import GatewayCallOptions, GatewayRequest, GatewayResponse


def build_request(
    prompt: str,
    *,
    schema: Optional[dict],
    options: Optional[GatewayCallOptions],
) -> GatewayRequest:
    """Build a gateway request from reusable shared options."""
    opts = options or GatewayCallOptions()
    return GatewayRequest(
        prompt=prompt,
        schema=schema,
        llm_provider=opts.llm_provider,
        llm_model=opts.llm_model,
        api_key=opts.api_key,
        base_url=opts.base_url,
        project_path=opts.project_path,
        config_ref=opts.config_ref,
        planner=opts.planner,
        timeout=opts.timeout,
        stream_callback=opts.stream_callback,
    )


def call_structured_prompt(
    *,
    prompt: str,
    schema: dict,
    options: Optional[GatewayCallOptions],
    call_structured_fn: Callable[[GatewayRequest], GatewayResponse],
) -> GatewayResponse:
    """Build + dispatch a structured gateway request."""
    request = build_request(prompt, schema=schema, options=options)
    return call_structured_fn(request)


def call_text_prompt(
    *,
    prompt: str,
    options: Optional[GatewayCallOptions],
    call_text_fn: Callable[[GatewayRequest], GatewayResponse],
) -> GatewayResponse:
    """Build + dispatch a free-form text gateway request."""
    request = build_request(prompt, schema=None, options=options)
    return call_text_fn(request)

