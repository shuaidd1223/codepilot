"""Filesystem layout helpers for CodePilot project artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping


def _slugify_project_name(text: str, *, fallback: str = "project") -> str:
    chars: list[str] = []
    last_dash = False
    for ch in (text or "").strip():
        if ch.isalnum() or ch in {"_", "-", "."}:
            chars.append(ch)
            last_dash = False
        elif not last_dash:
            chars.append("-")
            last_dash = True
    value = "".join(chars).strip(".-")
    return value[:80] or fallback


def project_storage_root(
    project: Mapping[str, object] | None = None,
    *,
    project_name: str | None = None,
    project_path: str | Path | None = None,
) -> Path:
    """Return the per-project CodePilot artifact root.

    The layout is intentionally data-first, then project-first:
    ``~/.codepilot/data/<project>/worktrees``, ``runs``, ``task-files``, etc.
    """
    name = (project_name or "").strip()
    path_value: str | Path | None = project_path

    if project is not None:
        name = name or str(project.get("name") or "").strip()
        path_value = path_value or project.get("path")  # type: ignore[assignment]

    if not name and path_value:
        name = Path(path_value).expanduser().name

    return Path.home() / ".codepilot" / "data" / _slugify_project_name(name)


def global_storage_root() -> Path:
    """Return the CodePilot root for cross-project state."""
    return Path.home() / ".codepilot"
