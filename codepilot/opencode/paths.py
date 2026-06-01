"""Filesystem paths for CodePilot's OpenCode runtime profile.

Only ``opencode.json`` (the main config with permission settings) may live at
the project level so users can see and edit it — the same pattern as
``.claude/settings.json`` for Claude Code.

All other generated files (tui.json, agents, instructions, commands, plugins)
and runtime data (SQLite database, XDG cache/state) stay in the user-global
``~/.codepilot/data/opencode/<scope>/`` directory.
"""

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


# ── user-global runtime root (for tool-generated files & XDG data) ───────────

def opencode_runtime_root(scope: str | None = None) -> Path:
    """Return the user-level runtime directory for OpenCode tool files & data.

    tui.json, agents/, instructions/, commands/, plugins/, XDG_DATA_HOME,
    XDG_CACHE_HOME, XDG_STATE_HOME and the SQLite database all live under
    this directory — never inside the project tree.
    """
    return global_storage_root() / "opencode" / _slugify_scope(scope or "default")


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


# ── project-level config (opencode.json only) ─────────────────────────────────

def opencode_runtime_config_path(
    scope: str | None = None,
    *,
    project_path: str | Path | None = None,
) -> Path:
    """Return the generated OpenCode config file path.

    When *project_path* is given, only ``opencode.json`` is written to
    ``.codepilot/opencode.json`` inside the project (visible and editable,
    analogous to ``.claude/settings.json``).

    Without *project_path* the config falls back to the user-global
    ``~/.codepilot/data/opencode/<scope>/opencode.json``.
    """
    if project_path:
        return Path(project_path).resolve() / ".codepilot" / "opencode.json"
    return opencode_runtime_root(scope) / "opencode.json"
