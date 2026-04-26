"""Execution helpers for AI gateway runners.

This layer performs the actual provider/CLI invocation after resolution has
already decided which call path to use.
"""

from __future__ import annotations

import subprocess
from typing import Any

from codepilot.gateway.resolution import (
    ResolvedStructuredCLICall,
    ResolvedTextCLICandidate,
)
from codepilot.gateway.types import GatewayRequest
from codepilot.core.text_decode import decode_subprocess_text


def execute_api_prompt(provider: Any, prompt: str) -> str:
    """Run one API prompt against a resolved provider instance."""
    from codepilot.ai_support.providers import _run_api_provider

    return _run_api_provider(provider, prompt)


def execute_structured_cli_call(
    request: GatewayRequest,
    resolved: ResolvedStructuredCLICall,
) -> dict:
    """Run schema-constrained CLI planning for the resolved backend family."""
    from codepilot.ai_support.service import _run_claude_schema_prompt, _run_codex_schema_prompt

    schema = request.schema or {}
    if resolved.cli_name == "claude":
        return _run_claude_schema_prompt(
            request.prompt,
            schema,
            planner=resolved.planner,
            project_path=request.project_path,
            config_ref=request.config_ref or None,
            timeout=request.timeout,
        )
    return _run_codex_schema_prompt(
        request.prompt,
        schema,
        project_path=request.project_path,
        config_ref=request.config_ref or None,
        timeout=request.timeout,
    )


def execute_text_cli_candidate(
    request: GatewayRequest,
    candidate: ResolvedTextCLICandidate,
) -> tuple[bool, str, str]:
    """Run one free-form text CLI candidate.

    Returns ``(ok, text, error)``.
    """
    try:
        result = subprocess.run(
            candidate.cmd,
            input=(request.prompt or "").encode("utf-8"),
            capture_output=True,
            text=False,
            timeout=request.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return False, "", f"{candidate.cli_name} invoke: {exc}"

    text = decode_subprocess_text(result.stdout).strip()
    stderr = decode_subprocess_text(result.stderr).strip()
    if result.returncode == 0 and text:
        return True, text, ""
    return (
        False,
        "",
        f"{candidate.cli_name} exit={result.returncode} stderr={stderr[:200]}",
    )

