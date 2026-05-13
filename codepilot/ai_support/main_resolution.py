"""Decision helpers for task-content generation in the AI facade.

This module resolves *what* path to use (API vs CLI) and prepares shared
request artifacts (prompt/env overrides) without invoking providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class ResolvedAPITaskContentCall:
    provider_key: str
    provider_ref: str | Path | dict | None
    prompt: str
    api_key_override: str | None


@dataclass(frozen=True)
class ResolvedCLITaskContentCall:
    provider_key: str
    provider_ref: str | Path | dict | None
    prompt: str
    env_overrides: dict[str, str]


TaskContentCall = ResolvedAPITaskContentCall | ResolvedCLITaskContentCall


def resolve_task_content_call(
    title: str,
    *,
    project_path: str = "",
    agent: str = "codex",
    api_keys: Optional[dict[str, str]] = None,
    config_ref: str | Path | dict | None = None,
    normalize_agent_name: Callable[[str], str],
    check_provider_availability: Callable[..., tuple[bool, str]],
    collect_project_context: Callable[[str], str],
    task_prompt_template: str,
    api_provider_keys: set[str],
    cli_provider_keys: set[str],
) -> TaskContentCall:
    """Resolve the content-generation call path and prepared request payload."""
    normalized = normalize_agent_name(agent)
    provider_ref = config_ref or project_path
    available, message = check_provider_availability(normalized, project_path=provider_ref)
    if not available:
        raise RuntimeError(message)
    if normalized == "dual":
        normalized = "codex"

    ctx = collect_project_context(project_path)
    prompt = task_prompt_template.format(title=title, project_context=ctx)

    env_overrides: dict[str, str] = {}
    if api_keys:
        for provider, key in api_keys.items():
            env_overrides[f"{provider.upper()}_API_KEY"] = key

    if normalized in api_provider_keys:
        return ResolvedAPITaskContentCall(
            provider_key=normalized,
            provider_ref=provider_ref,
            prompt=prompt,
            api_key_override=(api_keys or {}).get(normalized),
        )
    if normalized in cli_provider_keys:
        return ResolvedCLITaskContentCall(
            provider_key=normalized,
            provider_ref=provider_ref,
            prompt=prompt,
            env_overrides=env_overrides,
        )

    cli_list = ", ".join(sorted(cli_provider_keys))
    api_list = ", ".join(sorted(api_provider_keys))
    raise ValueError(
        f"未知的 agent: {agent} (normalized: {normalized})\n"
        f"支持的 CLI: {cli_list}\n"
        f"支持的 API: {api_list}"
    )

