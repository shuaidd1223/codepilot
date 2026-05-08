"""CLI agent family registry.

Single source of truth for which "CLI families" CodePilot can drive
(claude / codex / opencode). Both gateway resolution and ai_support
runtime layers read from this registry instead of maintaining parallel
if/else ladders or separate alias tables.

A family is the human-facing identity (``claude``); its ``provider_key``
points to the concrete CLI invocation descriptor in
``codepilot.ai_support.providers.CLI_PROVIDERS``. Keeping the two
separate lets us register multiple provider entries per family in the
future (e.g. claude vs claude-node) while still treating ``claude`` as
one logical family in the planner/fallback layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from codepilot.ai_support.opencode_runtime import (
    ANTHROPIC_PROVIDER_KEYS,
    OPENAI_PROVIDER_KEYS,
)


@dataclass(frozen=True)
class EnvBridge:
    """Declarative env mapping for a CLI family."""

    family_name: str
    target_var: str
    source_providers: tuple[str, ...] | str
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


@dataclass(frozen=True)
class CLIFamily:
    """Static descriptor for one CLI agent family.

    Attributes:
        name:         canonical identifier (claude / codex / opencode).
        provider_key: key into ``CLI_PROVIDERS`` for the actual invocation.
        env_var:      env var that overrides the family's command path
                      (e.g. ``CODEPILOT_CODEX_CMD``).
        aliases:      acceptable shorthand inputs (used by
                      ``normalize_agent_name``); must not collide with
                      another family's canonical name.
        is_planner:   true when this family participates in the structured
                      planner (schema-prompt) path. All three default
                      families are planners today.
        env_bridge:   optional API-key env bridge consumed by
                      ``family_runtime`` when native CLI auth is absent.
        bundled_path: optional executable path relative to the binary bundle
                      root for vendored CLI binaries.
    """

    name: str
    provider_key: str
    env_var: str
    aliases: tuple[str, ...] = ()
    is_planner: bool = True
    env_bridge: EnvBridge | None = None
    bundled_path: str = ""


CLI_FAMILIES: dict[str, CLIFamily] = {
    "claude": CLIFamily(
        name="claude",
        provider_key="claude",
        env_var="CODEPILOT_CLAUDE_CMD",
        aliases=("claude-code", "anthropic-claude"),
        env_bridge=EnvBridge(
            family_name="claude",
            target_var="ANTHROPIC_API_KEY",
            source_providers=ANTHROPIC_PROVIDER_KEYS,
            native_auth_paths=(_claude_auth_path,),
        ),
    ),
    "codex": CLIFamily(
        name="codex",
        provider_key="codex",
        env_var="CODEPILOT_CODEX_CMD",
        aliases=("openai-codex",),
        bundled_path="bin/vendor/codex",
        env_bridge=EnvBridge(
            family_name="codex",
            target_var="OPENAI_API_KEY",
            source_providers=OPENAI_PROVIDER_KEYS,
            native_auth_paths=(_codex_auth_path,),
        ),
    ),
    "opencode": CLIFamily(
        name="opencode",
        provider_key="opencode",
        env_var="CODEPILOT_OPENCODE_CMD",
        aliases=("oc", "open-code"),
        bundled_path="bin/vendor/opencode",
        env_bridge=EnvBridge(
            family_name="opencode",
            target_var="",
            source_providers="smart_pick",
            native_auth_paths=(_opencode_auth_path, _opencode_legacy_auth_path),
        ),
    ),
}


def all_family_names() -> list[str]:
    """Return the list of canonical family names in registration order."""
    return list(CLI_FAMILIES.keys())


def get_family(name: str) -> Optional[CLIFamily]:
    """Resolve a name (canonical or alias) to a :class:`CLIFamily`.

    Returns ``None`` when the input does not match any family. Comparison
    is case-insensitive and ignores leading/trailing whitespace.
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in CLI_FAMILIES:
        return CLI_FAMILIES[key]
    for fam in CLI_FAMILIES.values():
        if key in fam.aliases:
            return fam
    return None


def normalize_family_name(name: str) -> str:
    """Resolve to canonical family name; falls back to lowered input."""
    fam = get_family(name)
    if fam is not None:
        return fam.name
    return (name or "").strip().lower()


def planner_family_names() -> set[str]:
    """Names of families that participate in the structured planner path."""
    return {fam.name for fam in CLI_FAMILIES.values() if fam.is_planner}


def env_var_for(family_name: str) -> Optional[str]:
    """Return the env var that overrides ``family_name``'s command path."""
    fam = get_family(family_name)
    return fam.env_var if fam else None
