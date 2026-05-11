"""Filesystem paths for CodePilot's OpenCode runtime profile."""

from __future__ import annotations

from pathlib import Path

from codepilot.core.paths import global_storage_root


def _slugify_scope(value: str, *, fallback: str = "default") -> str:
    chars: list[str] = []
    last_dash = False
    for ch in str(value or "").strip():
        if ch.isalnum() or ch in {"_", "-", "."}:
            chars.append(ch)
            last_dash = False
        elif not last_dash:
            chars.append("-")
            last_dash = True
    return "".join(chars).strip(".-")[:80] or fallback


def opencode_runtime_root(scope: str | None = None) -> Path:
    """Return the user-level runtime directory for generated OpenCode files."""
    return global_storage_root() / "opencode" / _slugify_scope(scope or "default")


def opencode_runtime_config_path(scope: str | None = None) -> Path:
    """Return the generated OpenCode config file path for one runtime scope."""
    return opencode_runtime_root(scope) / "opencode.json"


def opencode_runtime_data_home(scope: str | None = None) -> Path:
    """Return the isolated XDG data home used by CodePilot-launched OpenCode."""
    return opencode_runtime_root(scope) / "xdg-data"


def opencode_runtime_cache_home(scope: str | None = None) -> Path:
    """Return the isolated XDG cache home used by CodePilot-launched OpenCode."""
    return opencode_runtime_root(scope) / "xdg-cache"


def opencode_runtime_state_home(scope: str | None = None) -> Path:
    """Return the isolated XDG state home used by CodePilot-launched OpenCode."""
    return opencode_runtime_root(scope) / "xdg-state"


def opencode_runtime_db_path(scope: str | None = None) -> Path:
    """Return the isolated OpenCode sqlite database path for one runtime scope."""
    return opencode_runtime_data_home(scope) / "opencode" / "opencode.db"
