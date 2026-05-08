"""Runtime env bridge for CLI families.

This module returns the minimal environment overrides needed for a family CLI
subprocess. If the CLI already has native auth on disk, no API key is injected.
"""

from __future__ import annotations

from codepilot.ai_support.cli_families import CLI_FAMILIES, EnvBridge, get_family
from codepilot.core.config import AgentsConfig, ProviderAPIConfig


class FamilyRuntimeUnavailable(RuntimeError):
    """Raised when a family has neither native auth nor a usable config key."""


FAMILY_ENV_BRIDGES: dict[str, EnvBridge] = {
    name: family.env_bridge
    for name, family in CLI_FAMILIES.items()
    if family.env_bridge is not None
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
    if isinstance(bridge.source_providers, str):
        raise FamilyRuntimeUnavailable(
            f"{bridge.family_name} 环境注入配置不可直接映射：{bridge.source_providers}"
        )
    for provider_name in bridge.source_providers:
        provider = _enabled_keyed_provider(cfg, provider_name)
        if provider is not None:
            return {bridge.target_var: provider.api_key.strip()}
    providers = " / ".join(bridge.source_providers)
    raise FamilyRuntimeUnavailable(
        f"{bridge.family_name} 环境注入不可用：未发现 native auth，"
        f"且 [providers.*] 中没有可用于 {bridge.target_var} 的 API key。"
        f"请配置 {providers} 任一 provider。"
    )


def _build_smart_pick_family_env(cfg: AgentsConfig) -> dict[str, str]:
    from codepilot.ai_support.opencode_runtime import select_opencode_backend

    return dict(select_opencode_backend(cfg).env)


def build_env_for_family(family_name: str, cfg: AgentsConfig) -> dict[str, str]:
    """Return only env vars that should be overlaid onto a family subprocess.

    Native auth wins because the CLI can authenticate by itself and injecting a
    config key would override user-local login state.
    """
    family = get_family(family_name)
    if family is None or family.env_bridge is None:
        raise FamilyRuntimeUnavailable(f"未知 CLI family：{family_name}")
    bridge = family.env_bridge

    if bridge.has_native_auth():
        return {}

    if bridge.source_providers == "smart_pick":
        return _build_smart_pick_family_env(cfg)

    return _build_direct_family_env(bridge, cfg)
