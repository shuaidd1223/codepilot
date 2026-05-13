"""Configuration-resolution helpers for AI gateway runners.

This layer decides *what* to call (provider / model / config_ref / fallback
candidate order) without performing any network or subprocess invocation.
The CLI family decisions (which families exist, which command they map to,
what their fallback ordering is) come from the family registry in
:mod:`codepilot.ai_support.cli_families`, so adding a new family does not
require touching this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from codepilot.ai_support.cli_families import CLI_FAMILIES, get_family
from codepilot.ai_support.providers import mark_provider_unavailable
from codepilot.gateway.types import GatewayRequest


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


def _planner_to_family_name(planner: str) -> str:
    """Map a free-form planner name to the canonical CLI family name.

    Falls back to ``codex`` when the input does not match any known family,
    matching the legacy default for unspecified planners.
    """
    if not planner:
        return "codex"
    fam = get_family(planner)
    if fam is not None:
        return fam.name
    # Unrecognised inputs (e.g. claude-sonnet) — try to match by prefix.
    lowered = planner.strip().lower()
    for fam_name in CLI_FAMILIES:
        if lowered.startswith(fam_name + "-") or lowered.startswith(fam_name):
            return fam_name
    return "codex"


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
        if hasattr(provider, "auto_model_selection"):
            provider.auto_model_selection = False
    if request.api_key:
        provider.api_key = request.api_key
    if request.base_url:
        provider.base_url = request.base_url
    if provider.requires_api_key() and not provider.resolve_api_key():
        mark_provider_unavailable(
            provider_key,
            provider,
            "missing api key",
            source="gateway",
            project_path=request.project_path or request.config_ref or "",
        )
        return None

    return ResolvedAPICall(
        provider_key=provider_key,
        provider=provider,
        source=f"api:{provider_key}",
    )


def resolve_structured_cli_call(request: GatewayRequest) -> ResolvedStructuredCLICall:
    """Resolve which structured CLI family should run for the request."""
    family_name = _planner_to_family_name(request.planner or "codex")
    planner = request.planner or family_name
    return ResolvedStructuredCLICall(
        cli_name=family_name,
        planner=planner,
        source=f"cli:{family_name}",
    )


def _build_text_cli_command(
    *,
    cli_name: str,
    executable: str,
    project_path: str,
) -> list[str]:
    """Build the headless text-mode invocation for one CLI family."""
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
    if cli_name == "opencode":
        # `opencode run` reads the prompt from STDIN when no positional arg is given,
        # which matches the gateway text-mode runner that pipes via subprocess input=.
        return [executable, "run"]
    # claude (and any other family with the standard flag set)
    return [
        executable,
        "-p",
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ]


def _resolve_fallback_order(request: GatewayRequest) -> list[str]:
    """Return the de-duplicated fallback CLI family order to try.

    Reads from ``[automation] fallback_cli_order`` (default
    ``["claude", "codex", "opencode"]``). The caller's ``planner`` field
    only influences :func:`resolve_structured_cli_call`; the text-mode
    fallback chain is config-driven so the user controls priority via
    AGENTS.toml without per-call gymnastics.
    """
    from codepilot.core.config import DEFAULT_FALLBACK_CLI_ORDER, load_project_config

    configured: list[str] = []
    try:
        cfg = load_project_config(_provider_ref(request))
    except Exception:
        cfg = None
    if cfg is not None:
        configured = list(cfg.automation.fallback_cli_order or [])
    if not configured:
        configured = list(DEFAULT_FALLBACK_CLI_ORDER)

    order: list[str] = []
    for name in configured:
        if name not in order and name in CLI_FAMILIES:
            order.append(name)
    return order


def resolve_text_cli_candidates(request: GatewayRequest) -> tuple[list[ResolvedTextCLICandidate], str]:
    """Resolve runnable local CLI candidates for free-form text mode.

    Reads the candidate order from the family registry plus the configured
    ``fallback_cli_order``; each family's executable lookup happens via
    ``resolve_cli_provider`` so missing CLIs degrade gracefully.
    """
    from codepilot.ai_support.providers import resolve_cli_provider  # noqa: WPS433

    provider_ref = _provider_ref(request)
    order = _resolve_fallback_order(request)

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
