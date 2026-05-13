"""OpenCode CLI runtime support.

OpenCode (sst/opencode) is provider-agnostic — it speaks Anthropic / OpenAI /
OpenAI-compatible endpoints depending on which env vars are populated when the
subprocess starts. This module is the bridge between CodePilot's
``[providers.*]`` config and that env-driven contract.

Selection rule (smart auto-pick):

  1. anthropic         — any of ``claude-opus`` / ``claude-sonnet`` / ``claude-haiku``
                         providers has a non-empty ``api_key`` and is enabled.
                         Injects ``ANTHROPIC_API_KEY``.
  2. openai-compatible — ``deepseek`` provider has a key + ``base_url``.
                         Injects ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL``.
  3. openai            — ``openai`` or any ``openai-*`` provider has a key.
                         Injects ``OPENAI_API_KEY`` (+ ``OPENAI_BASE_URL`` only if
                         the provider has a custom ``base_url``).

If none of the above match, :func:`select_opencode_backend` raises
:class:`OpenCodeBackendUnavailable` with an actionable hint so callers can
surface a clear "OpenCode 兜底未启用" diagnostic instead of a generic
subprocess failure later on.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codepilot.core.config import AgentsConfig, ProviderAPIConfig

ANTHROPIC_PROVIDER_KEYS = ("claude-opus", "claude-sonnet", "claude-haiku")
OPENAI_PROVIDER_KEYS = ("openai", "openai-gpt4", "openai-gpt4o", "openai-gpt35")
DEEPSEEK_PROVIDER_KEY = "deepseek"


class OpenCodeBackend(enum.Enum):
    """Discriminator for which API style OpenCode should target."""

    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai-compatible"
    OPENAI = "openai"


class OpenCodeBackendUnavailable(RuntimeError):
    """Raised when no [providers.*] entry has a key OpenCode can use."""


@dataclass(frozen=True)
class OpenCodeBackendSelection:
    """Resolved backend choice + the env vars that should be injected."""

    name: str
    env: dict[str, str]
    source_provider: str


def _enabled_provider(cfg: AgentsConfig, name: str) -> ProviderAPIConfig | None:
    provider = cfg.providers.get(name)
    if provider is None:
        return None
    if not provider.enabled:
        return None
    if not (provider.api_key or "").strip():
        return None
    return provider


def select_opencode_backend(cfg: AgentsConfig) -> OpenCodeBackendSelection:
    """Pick the best backend for OpenCode based on configured provider keys.

    Implements the auto-pick chain documented in the module docstring. The
    return value carries both the discriminator and the env-var dict so the
    caller can compose it with the inherited subprocess environment.
    """
    for name in ANTHROPIC_PROVIDER_KEYS:
        provider = _enabled_provider(cfg, name)
        if provider is not None:
            return OpenCodeBackendSelection(
                name=OpenCodeBackend.ANTHROPIC.name,
                env={"ANTHROPIC_API_KEY": provider.api_key.strip()},
                source_provider=name,
            )

    deepseek = _enabled_provider(cfg, DEEPSEEK_PROVIDER_KEY)
    if deepseek is not None:
        env = {"OPENAI_API_KEY": deepseek.api_key.strip()}
        base_url = (deepseek.base_url or "").strip()
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
        return OpenCodeBackendSelection(
            name=OpenCodeBackend.OPENAI_COMPATIBLE.name,
            env=env,
            source_provider=DEEPSEEK_PROVIDER_KEY,
        )

    for name in OPENAI_PROVIDER_KEYS:
        provider = _enabled_provider(cfg, name)
        if provider is not None:
            env = {"OPENAI_API_KEY": provider.api_key.strip()}
            base_url = (provider.base_url or "").strip()
            if base_url:
                env["OPENAI_BASE_URL"] = base_url
            return OpenCodeBackendSelection(
                name=OpenCodeBackend.OPENAI.name,
                env=env,
                source_provider=name,
            )

    raise OpenCodeBackendUnavailable(
        "OpenCode 兜底未启用：[providers.*] 里没有可用的 API key。\n"
        "请在 AGENTS.toml / .codepilot.secrets.toml 里至少配置以下任一项："
        "anthropic（claude-opus/sonnet/haiku 任意一个），"
        "deepseek（含 base_url），"
        "或 openai / openai-gpt4 / openai-gpt4o / openai-gpt35。"
    )


def build_opencode_env(cfg: AgentsConfig) -> dict[str, str]:
    """Return only the env vars OpenCode needs — no other provider keys leak in.

    The caller is expected to merge this dict on top of the inherited process
    environment when launching ``opencode run``.
    """
    from codepilot.ai_support.family_runtime import build_env_for_family

    return build_env_for_family("opencode", cfg)


def wrap_schema_prompt(prompt: str, schema: dict) -> str:
    """Bake a JSON-schema instruction header onto the user prompt.

    OpenCode does not currently expose a native ``--output-schema`` flag, so
    schema-constrained planning is enforced through prompt engineering. The
    caller is expected to parse the model's response with the project's
    standard structured-JSON parser (which already tolerates code-fence and
    leading prose).
    """
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
    return (
        "你必须只输出一个 JSON 对象，符合下述 JSON Schema，不要包含任何额外文本、"
        "Markdown 代码块标记或解释：\n\n"
        "Schema:\n"
        f"{schema_json}\n\n"
        "请基于以下需求生成 JSON：\n\n"
        f"{prompt}\n"
    )


def run_opencode_schema_prompt(*args: Any, **kwargs: Any) -> dict:
    """Re-export of the shared planner_execution implementation.

    Kept here so callers can import the OpenCode-specific runtime through
    one cohesive module while the actual subprocess wiring lives next to
    its claude/codex siblings in :mod:`planner_execution`.
    """
    from codepilot.ai_support.planner_execution import (
        run_opencode_schema_prompt as _impl,
    )

    return _impl(*args, **kwargs)
