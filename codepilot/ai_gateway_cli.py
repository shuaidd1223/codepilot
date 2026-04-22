"""CLI-runner specific behaviour for the AI gateway."""

from __future__ import annotations

import subprocess

from codepilot.ai_gateway_types import GatewayRequest, GatewayResponse


def try_cli_structured(request: GatewayRequest) -> GatewayResponse:
    """Invoke local CLI planner with a JSON schema contract."""
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


def try_cli_text(request: GatewayRequest) -> GatewayResponse:
    """Invoke local CLI planner expecting free-form text output."""
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
        last_error = f"{cli_name} exit={result.returncode} stderr={(result.stderr or '').strip()[:200]}"

    return GatewayResponse(
        ok=False,
        source="cli:none",
        error=last_error or "no local CLI available",
    )
