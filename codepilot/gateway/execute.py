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
    """Run schema-constrained CLI planning for the resolved backend family.

    Dispatches via the CLI family registry: each registered family points at
    its own ``_run_<family>_schema_prompt`` thin wrapper in
    :mod:`codepilot.ai_support.service`. Adding a new family means
    registering it once in ``cli_families.py`` and providing a matching
    ``service._run_<family>_schema_prompt`` callable.
    """
    from codepilot.ai_support import service as service_mod
    from codepilot.ai_support.cli_families import CLI_FAMILIES

    schema = request.schema or {}
    if resolved.cli_name not in CLI_FAMILIES:
        raise ValueError(
            f"未知的 CLI family: {resolved.cli_name!r}。"
            "可用 family 在 codepilot.ai_support.cli_families.CLI_FAMILIES 注册。"
        )

    runner_attr = f"_run_{resolved.cli_name}_schema_prompt"
    runner = getattr(service_mod, runner_attr, None)
    if runner is None:
        raise RuntimeError(
            f"CLI family {resolved.cli_name!r} 已注册，但缺少 service.{runner_attr}。"
            "请在 codepilot/ai_support/service.py 补充对应薄壳。"
        )

    if resolved.cli_name == "claude":
        # Claude's planner sub-family (claude-sonnet/opus/haiku) is preserved
        # so the runner can pick the right --model alias.
        return runner(
            request.prompt,
            schema,
            planner=resolved.planner,
            project_path=request.project_path,
            config_ref=request.config_ref or None,
            timeout=request.timeout,
        )

    return runner(
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

