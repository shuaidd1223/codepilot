"""Runtime env bridge for CLI families.

This module returns the minimal environment overrides needed for a family CLI
subprocess. If the CLI already has native auth on disk, no API key is injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from codepilot.ai_support.opencode_runtime import (
    ANTHROPIC_PROVIDER_KEYS,
    OPENAI_PROVIDER_KEYS,
    build_opencode_env,
)
from codepilot.core.config import AgentsConfig, ProviderAPIConfig


class FamilyRuntimeUnavailable(RuntimeError):
    """Raised when a family has neither native auth nor a usable config key."""


@dataclass(frozen=True)
class EnvBridge:
    """Declarative env mapping for a CLI family."""

    family_name: str
    target_env_var: str
    source_providers: tuple[str, ...]
    native_auth_paths: tuple[Callable[[], Path], ...]

    def has_native_auth(self) -> bool:
        return any(path().is_file() for path in self.native_auth_paths)


def _home_path(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def _claude_auth_path() -> Path:
    return _home_path(".claude", "auth.json")


def _codex_auth_path() -> Path:
    return _home_path(".codex", "auth.json")


def _opencode_auth_path() -> Path:
    return _home_path(".local", "share", "opencode", "auth.json")


def _opencode_legacy_auth_path() -> Path:
    return _home_path(".opencode", "data", "auth.json")


FAMILY_ENV_BRIDGES: dict[str, EnvBridge] = {
    "claude": EnvBridge(
        family_name="claude",
        target_env_var="ANTHROPIC_API_KEY",
        source_providers=ANTHROPIC_PROVIDER_KEYS,
        native_auth_paths=(_claude_auth_path,),
    ),
    "codex": EnvBridge(
        family_name="codex",
        target_env_var="OPENAI_API_KEY",
        source_providers=OPENAI_PROVIDER_KEYS,
        native_auth_paths=(_codex_auth_path,),
    ),
    "opencode": EnvBridge(
        family_name="opencode",
        target_env_var="",
        source_providers=(),
        native_auth_paths=(_opencode_auth_path, _opencode_legacy_auth_path),
    ),
}


def _enabled_keyed_provider(cfg: AgentsConfig, name: str) -> ProviderAPIConfig | None:
    provider = cfg.providers.get(name)
    if provider is None:
        return None
    if not provider.enabled:
        return None
    if not (provider.api_key or "").strip():
        return None
    return provider


def _build_direct_family_env(bridge: EnvBridge, cfg: AgentsConfig) -> dict[str, str]:
    for provider_name in bridge.source_providers:
        provider = _enabled_keyed_provider(cfg, provider_name)
        if provider is not None:
            return {bridge.target_env_var: provider.api_key.strip()}
    providers = " / ".join(bridge.source_providers)
    raise FamilyRuntimeUnavailable(
        f"{bridge.family_name} 环境注入不可用：未发现 native auth，"
        f"且 [providers.*] 中没有可用于 {bridge.target_env_var} 的 API key。"
        f"请配置 {providers} 任一 provider。"
    )


def build_env_for_family(family_name: str, cfg: AgentsConfig) -> dict[str, str]:
    """Return only env vars that should be overlaid onto a family subprocess.

    Native auth wins because the CLI can authenticate by itself and injecting a
    config key would override user-local login state.
    """
    normalized = str(family_name or "").strip().lower()
    bridge = FAMILY_ENV_BRIDGES.get(normalized)
    if bridge is None:
        raise FamilyRuntimeUnavailable(f"未知 CLI family：{family_name}")

    if bridge.has_native_auth():
        return {}

    if normalized == "opencode":
        return build_opencode_env(cfg)

    return _build_direct_family_env(bridge, cfg)
