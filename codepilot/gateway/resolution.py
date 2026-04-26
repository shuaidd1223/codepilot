"""Configuration-resolution helpers for AI gateway runners.

This layer decides *what* to call (provider / model / config_ref / fallback
candidate order) without performing any network or subprocess invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from codepilot.gateway.types import GatewayRequest


_CLAUDE_PLANNERS = {
    "claude",
    "claude-node",
    "claude-sonnet",
    "claude-opus",
    "claude-haiku",
}


@dataclass
class ResolvedAPICall:
    provider_key: str
    provider: Any
    source: str


@dataclass(frozen=True)
class ResolvedStructuredCLICall:
    cli_name: str
    planner: str
    source: str


@dataclass(frozen=True)
class ResolvedTextCLICandidate:
    cli_name: str
    source: str
    cmd: list[str]


def _provider_ref(request: GatewayRequest) -> str | None:
    return request.config_ref or request.project_path or None


def resolve_api_call(request: GatewayRequest) -> Optional[ResolvedAPICall]:
    """Resolve an API call candidate; return ``None`` when API path is not usable."""
    if not request.classifier_provider:
        return None

    from codepilot.ai_support.providers import API_PROVIDERS, resolve_api_provider

    provider_key = request.classifier_provider
    if provider_key not in API_PROVIDERS:
        return None

    provider = resolve_api_provider(provider_key, _provider_ref(request))
    if request.classifier_model:
        provider.model = request.classifier_model
    if request.api_key:
        provider.api_key = request.api_key
    if request.base_url:
        provider.base_url = request.base_url
    if provider.requires_api_key() and not provider.resolve_api_key():
        return None

    return ResolvedAPICall(
        provider_key=provider_key,
        provider=provider,
        source=f"api:{provider_key}",
    )


def resolve_structured_cli_call(request: GatewayRequest) -> ResolvedStructuredCLICall:
    """Resolve which structured CLI family should run for the request."""
    from codepilot.ai_support.service import normalize_agent_name  # noqa: WPS433

    normalized = normalize_agent_name(request.planner) if request.planner else "codex"
    if normalized in _CLAUDE_PLANNERS:
        return ResolvedStructuredCLICall(
            cli_name="claude",
            planner=normalized,
            source="cli:claude",
        )
    return ResolvedStructuredCLICall(
        cli_name="codex",
        planner="codex",
        source="cli:codex",
    )


def _build_text_cli_command(
    *,
    cli_name: str,
    executable: str,
    project_path: str,
) -> list[str]:
    if cli_name == "codex":
        cmd = [
            executable,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if project_path:
            cmd = [executable, "-C", project_path] + cmd[1:]
        return cmd
    return [
        executable,
        "-p",
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ]


def resolve_text_cli_candidates(request: GatewayRequest) -> tuple[list[ResolvedTextCLICandidate], str]:
    """Resolve runnable local CLI candidates for free-form text mode."""
    from codepilot.ai_support.service import normalize_agent_name  # noqa: WPS433
    from codepilot.ai_support.providers import resolve_cli_provider  # noqa: WPS433

    normalized = normalize_agent_name(request.planner) if request.planner else "codex"
    provider_ref = _provider_ref(request)

    order: list[str] = []
    if normalized in _CLAUDE_PLANNERS:
        order.append("claude")
    if "claude" not in order:
        order.append("claude")
    order.append("codex")

    candidates: list[ResolvedTextCLICandidate] = []
    last_error = ""
    for cli_name in order:
        try:
            provider = resolve_cli_provider(cli_name, provider_ref)
            exe = provider.find_executable()
        except Exception as exc:  # noqa: BLE001
            last_error = f"resolve {cli_name}: {exc}"
            continue
        if not exe:
            last_error = f"{cli_name} CLI not installed"
            continue

        candidates.append(
            ResolvedTextCLICandidate(
                cli_name=cli_name,
                source=f"cli:{cli_name}",
                cmd=_build_text_cli_command(
                    cli_name=cli_name,
                    executable=str(exe),
                    project_path=request.project_path,
                ),
            )
        )

    return candidates, last_error


