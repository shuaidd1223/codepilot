"""Unified surface for AI calls.

Historical state: `ai_classifier.classify_intent`, `ai_clarify.assess_requirement`,
and `ai_classifier.answer_question_via_api` each reimplemented the same
plumbing — probe the configured API provider, fall back to a local CLI
planner, parse JSON, wrap errors. The bodies were similar but diverged
slightly (different JSON-envelope strippers, different CLI preference
order, different error messages), which made consistent improvements
(timeouts, key resolution, logging) impossible without touching every
callsite.

This module consolidates that routing behind two entry points:

* :func:`call_structured` — prompt + JSON schema → parsed ``dict``.
* :func:`call_text` — prompt → free-form ``str`` response.

Both honour the same priority order:

1. Configured API provider (if ``classifier_provider`` is set and keys
   resolve).
2. Local CLI planner (``claude`` family by default, ``codex`` as final
   fallback). Structured calls use the CLI providers' ``--json-schema``
   mode via ``_run_claude_schema_prompt`` / ``_run_codex_schema_prompt``;
   text calls pipe the prompt through stdin and capture stdout.

The old helpers keep working — the gateway delegates to them rather than
duplicating the CLI invocation logic. That means the provider discovery,
env-var setup, and timeout handling stay centralised in ``ai_providers``
and ``ai.py``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional


# ═══════════════════════════════════════════════════════════════════════════
#   Public request / response types
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class GatewayRequest:
    prompt: str
    schema: Optional[dict] = None
    classifier_provider: str = ""
    classifier_model: str = ""
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    project_path: str = ""
    config_ref: str = ""
    planner: str = "codex"  # CLI fallback family
    timeout: int = 60


@dataclass
class GatewayResponse:
    ok: bool
    source: str  # "api:<key>" | "cli:claude" | "cli:codex" | "default"
    payload: Optional[dict] = None   # populated when schema was set
    text: str = ""                   # populated when schema was None
    error: str = ""                  # non-empty when ok is False


@dataclass(frozen=True)
class _GatewayMode:
    """Mode-specific pieces while sharing one routing skeleton."""

    name: str
    schema_required: bool
    schema_error: str
    api_formatter: Callable[[str, str], GatewayResponse]


# ═══════════════════════════════════════════════════════════════════════════
#   JSON envelope normalisation
# ═══════════════════════════════════════════════════════════════════════════


def _strip_json_envelope(raw: str) -> str:
    """Best-effort recovery of a JSON body from a possibly chatty response.

    LLMs often wrap JSON in ``` fences or prefix it with a sentence. We
    strip common markers and collapse to the first '{' ... last '}' slice
    so ``json.loads`` has a chance.
    """
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start : end + 1]
    return raw.strip()


# ═══════════════════════════════════════════════════════════════════════════
#   Internal helpers — chose a path, not the work
# ═══════════════════════════════════════════════════════════════════════════


def _try_api(request: GatewayRequest, mode: _GatewayMode) -> Optional[GatewayResponse]:
    """Invoke the configured API provider. Return None if not configured;
    return a failed GatewayResponse only when the provider was configured
    but raised (so the caller can decide whether to fall back)."""
    if not request.classifier_provider:
        return None

    from codepilot.ai_providers import API_PROVIDERS, _run_api_provider, resolve_api_provider

    if request.classifier_provider not in API_PROVIDERS:
        return None

    provider_ref = request.config_ref or request.project_path or None
    provider = resolve_api_provider(request.classifier_provider, provider_ref)
    if request.classifier_model:
        provider.model = request.classifier_model
    if request.api_key:
        provider.api_key = request.api_key
    if request.base_url:
        provider.base_url = request.base_url
    if provider.requires_api_key() and not provider.resolve_api_key():
        return None  # key missing → silently skip, try CLI

    try:
        raw = _run_api_provider(provider, request.prompt)
    except Exception as exc:  # noqa: BLE001 — caller expects string error
        return GatewayResponse(
            ok=False,
            source=f"api:{request.classifier_provider}",
            error=f"api provider error: {exc}",
        )

    source = f"api:{request.classifier_provider}"
    return mode.api_formatter(raw, source)


def _try_cli_structured(request: GatewayRequest) -> GatewayResponse:
    """Invoke the local CLI planner with a JSON schema contract."""
    # Imported lazily: ai.py also imports this module transitively.
    from codepilot.ai import (  # noqa: WPS433
        _run_claude_schema_prompt,
        _run_codex_schema_prompt,
        normalize_agent_name,
    )

    normalized = normalize_agent_name(request.planner) if request.planner else "codex"
    schema = request.schema or {}

    if normalized in {"claude", "claude-node", "claude-sonnet", "claude-opus", "claude-haiku"}:
        try:
            payload = _run_claude_schema_prompt(
                request.prompt,
                schema,
                planner=normalized,
                project_path=request.project_path,
                config_ref=request.config_ref or None,
                timeout=request.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return GatewayResponse(
                ok=False,
                source="cli:claude",
                error=f"claude CLI error: {exc}",
            )
        return GatewayResponse(ok=True, source="cli:claude", payload=payload)

    try:
        payload = _run_codex_schema_prompt(
            request.prompt,
            schema,
            project_path=request.project_path,
            config_ref=request.config_ref or None,
            timeout=request.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return GatewayResponse(
            ok=False,
            source="cli:codex",
            error=f"codex CLI error: {exc}",
        )
    return GatewayResponse(ok=True, source="cli:codex", payload=payload)


def _try_cli_text(request: GatewayRequest) -> GatewayResponse:
    """Invoke the local CLI planner expecting free-form text output.

    Mirrors the ``_answer_via_local_cli`` helper in ai_classifier but
    parameterised by ``planner`` so the gateway can honour the caller's
    preferred CLI family.
    """
    from codepilot.ai import normalize_agent_name  # noqa: WPS433
    from codepilot.ai_providers import resolve_cli_provider  # noqa: WPS433

    normalized = normalize_agent_name(request.planner) if request.planner else "codex"
    provider_ref = request.config_ref or request.project_path or None

    # Prefer the caller's chosen family, then claude, then codex.
    order: list[str] = []
    if normalized in {"claude", "claude-node", "claude-sonnet", "claude-opus", "claude-haiku"}:
        order.append("claude")
    if "claude" not in order:
        order.append("claude")
    order.append("codex")

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

        if cli_name == "codex":
            cmd = [
                str(exe),
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--dangerously-bypass-approvals-and-sandbox",
            ]
            if request.project_path:
                cmd = [str(exe), "-C", request.project_path] + cmd[1:]
        else:
            cmd = [
                str(exe),
                "-p",
                "--output-format",
                "text",
                "--dangerously-skip-permissions",
            ]

        try:
            result = subprocess.run(
                cmd,
                input=request.prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=request.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = f"{cli_name} invoke: {exc}"
            continue

        text = (result.stdout or "").strip()
        if result.returncode == 0 and text:
            return GatewayResponse(ok=True, source=f"cli:{cli_name}", text=text)
        last_error = (
            f"{cli_name} exit={result.returncode} stderr={(result.stderr or '').strip()[:200]}"
        )

    return GatewayResponse(
        ok=False,
        source="cli:none",
        error=last_error or "no local CLI available",
    )


def _format_api_structured(raw: str, source: str) -> GatewayResponse:
    try:
        payload = json.loads(_strip_json_envelope(raw))
    except Exception as exc:  # noqa: BLE001
        return GatewayResponse(
            ok=False,
            source=source,
            error=f"api provider returned non-JSON: {exc}",
        )
    return GatewayResponse(ok=True, source=source, payload=payload)


def _format_api_text(raw: str, source: str) -> GatewayResponse:
    return GatewayResponse(ok=True, source=source, text=raw.strip())


_STRUCTURED_MODE = _GatewayMode(
    name="structured",
    schema_required=True,
    schema_error="call_structured requires a schema; use call_text for free-form output",
    api_formatter=_format_api_structured,
)
_TEXT_MODE = _GatewayMode(
    name="text",
    schema_required=False,
    schema_error="call_text is for free-form output; use call_structured with a schema",
    api_formatter=_format_api_text,
)


# ═══════════════════════════════════════════════════════════════════════════
#   Public entry points
# ═══════════════════════════════════════════════════════════════════════════


def call_structured(request: GatewayRequest) -> GatewayResponse:
    """Run a prompt expecting a JSON payload matching ``request.schema``.

    The schema is advisory for the API path (provider decides how to enforce
    it) and authoritative for the CLI path (passed as ``--json-schema``).
    """
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
    """Single fallback decision entry for API → CLI routing.

    This keeps the fallback behavior identical for both structured and text
    calls: try API first (when configured), then fall through to CLI, and
    surface a merged error tail when both paths fail.
    """
    if mode.schema_required and request.schema is None:
        raise ValueError(mode.schema_error)
    if not mode.schema_required and request.schema is not None:
        raise ValueError(mode.schema_error)

    api_result = _try_api(request, mode)
    if api_result is not None and api_result.ok:
        return api_result

    cli_result = cli_runner(request)
    if cli_result.ok:
        return cli_result

    api_err = api_result.error if api_result is not None else ""
    combined = "; ".join(err for err in (api_err, cli_result.error) if err)
    return GatewayResponse(ok=False, source=cli_result.source, error=combined)
