"""Execution helpers for resolved AI task-content calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from codepilot.ai_support.main_resolution import (
    ResolvedAPITaskContentCall,
    ResolvedCLITaskContentCall,
    TaskContentCall,
)


def _execute_api_task_content(
    resolved: ResolvedAPITaskContentCall,
    *,
    resolve_api_provider: Callable[..., Any],
    run_api_provider: Callable[[Any, str], str],
) -> str:
    provider = resolve_api_provider(resolved.provider_key, resolved.provider_ref)
    if resolved.api_key_override:
        provider.api_key = resolved.api_key_override
    return run_api_provider(provider, resolved.prompt)


def _execute_cli_task_content(
    resolved: ResolvedCLITaskContentCall,
    *,
    resolve_cli_provider: Callable[..., Any],
    run_cli_provider: Callable[[Any, str, dict[str, str] | None], str],
    get_node_modules_path: Callable[[], str],
) -> str:
    provider = resolve_cli_provider(resolved.provider_key, resolved.provider_ref)

    if resolved.provider_key == "claude-node":
        node_modules = get_node_modules_path()
        cli_js = Path(node_modules, "@anthropic-ai", "claude-code", "cli.js")
        if not cli_js.exists():
            raise RuntimeError(
                f"未找到 Claude Code CLI: {node_modules}/@anthropic-ai/claude-code/cli.js\n"
                "请运行: npm install -g @anthropic-ai/claude-code"
            )

    return run_cli_provider(provider, resolved.prompt, resolved.env_overrides)


def execute_task_content_call(
    resolved: TaskContentCall,
    *,
    resolve_api_provider: Callable[..., Any],
    resolve_cli_provider: Callable[..., Any],
    run_api_provider: Callable[[Any, str], str],
    run_cli_provider: Callable[[Any, str, dict[str, str] | None], str],
    get_node_modules_path: Callable[[], str],
) -> str:
    """Execute one resolved task-content call."""
    if isinstance(resolved, ResolvedAPITaskContentCall):
        return _execute_api_task_content(
            resolved,
            resolve_api_provider=resolve_api_provider,
            run_api_provider=run_api_provider,
        )
    return _execute_cli_task_content(
        resolved,
        resolve_cli_provider=resolve_cli_provider,
        run_cli_provider=run_cli_provider,
        get_node_modules_path=get_node_modules_path,
    )


