from __future__ import annotations

from codepilot.gateway.types import GatewayCallOptions, GatewayRequest, GatewayResponse


def make_gateway_options(
    *,
    classifier_provider: str = "openai",
    classifier_model: str = "gpt-x",
    api_key: str = "sk-1",
    base_url: str = "https://models.example.invalid/v1",
    project_path: str = "D:/demo/project",
    config_ref: str = "D:/demo/config/AGENTS.toml",
    planner: str = "claude",
    timeout: int = 42,
) -> GatewayCallOptions:
    return GatewayCallOptions(
        classifier_provider=classifier_provider,
        classifier_model=classifier_model,
        api_key=api_key,
        base_url=base_url,
        project_path=project_path,
        config_ref=config_ref,
        planner=planner,
        timeout=timeout,
    )


def assert_request_matches_options(
    request: GatewayRequest,
    *,
    prompt: str,
    schema: dict | None,
    options: GatewayCallOptions,
) -> None:
    assert request.prompt == prompt
    assert request.schema == schema
    assert request.classifier_provider == options.classifier_provider
    assert request.classifier_model == options.classifier_model
    assert request.api_key == options.api_key
    assert request.base_url == options.base_url
    assert request.project_path == options.project_path
    assert request.config_ref == options.config_ref
    assert request.planner == options.planner
    assert request.timeout == options.timeout


def assert_failure_response(
    response: GatewayResponse,
    *,
    source: str,
    error_exact: str | None = None,
    error_contains: tuple[str, ...] = (),
) -> None:
    assert response.ok is False
    assert response.source == source
    if error_exact is not None:
        assert response.error == error_exact
    for fragment in error_contains:
        assert fragment in response.error

