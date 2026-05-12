"""Locate Claude Code session transcripts on disk.

Claude Code records every conversation as ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl``.
The encoded directory replaces ``:``, ``\\``, and ``/`` with ``-`` so a Windows path like
``D:\\myCode\\workflow`` becomes ``D--myCode-workflow``.  This module exposes helpers to map a
project path to that encoding and to pick the most recently updated session id so the
CodePilot chat wrapper can offer a unified ``codepilot chat --session <id>`` resume hint
after Claude's TUI exits.
"""

from __future__ import annotations

from pathlib import Path


CLAUDE_PROJECTS_ENCODED_SEPARATORS = (":", "\\", "/")


def encode_claude_project_dir(project_path: str | Path) -> str:
    """Return the directory name Claude Code uses for ``project_path``."""
    text = str(project_path)
    for char in CLAUDE_PROJECTS_ENCODED_SEPARATORS:
        text = text.replace(char, "-")
    return text


def default_claude_projects_root() -> Path:
    """Return the standard ``~/.claude/projects`` directory."""
    return Path.home() / ".claude" / "projects"


def latest_claude_session_id(
    project_path: str | Path,
    *,
    projects_root: str | Path | None = None,
) -> str:
    """Return the most recently modified Claude session id for ``project_path``.

    Returns an empty string when no transcripts are present or the project directory
    has never been opened by Claude Code.
    """
    root = Path(projects_root) if projects_root is not None else default_claude_projects_root()
    if not root.is_dir():
        return ""
    encoded = encode_claude_project_dir(Path(project_path))
    target = _resolve_project_dir(root, encoded)
    if target is None:
        return ""
    transcripts = [item for item in target.iterdir() if item.is_file() and item.suffix == ".jsonl"]
    if not transcripts:
        return ""
    latest = max(transcripts, key=lambda item: item.stat().st_mtime)
    return latest.stem


def claude_session_exists(
    project_path: str | Path,
    session_id: str,
    *,
    projects_root: str | Path | None = None,
) -> bool:
    """Return True when Claude has a transcript for ``session_id`` under ``project_path``."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    root = Path(projects_root) if projects_root is not None else default_claude_projects_root()
    if not root.is_dir():
        return False
    target = _resolve_project_dir(root, encode_claude_project_dir(Path(project_path)))
    if target is None:
        return False
    return (target / f"{sid}.jsonl").is_file()


def _resolve_project_dir(root: Path, encoded: str) -> Path | None:
    candidate = root / encoded
    if candidate.is_dir():
        return candidate
    lowered = encoded.lower()
    try:
        children = list(root.iterdir())
    except OSError:
        return None
    for child in children:
        if child.is_dir() and child.name.lower() == lowered:
            return child
    return None
