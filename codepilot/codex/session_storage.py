"""Locate Codex CLI session transcripts on disk.

Codex stores every conversation as ``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl``.
The first line of each transcript is a ``session_meta`` event whose ``payload`` carries the
session ``id`` and the working directory (``cwd``).  Match by id alone is unsafe — Codex never
checks ``cwd`` when ``codex resume`` is invoked, so we anchor every lookup to the project path
to keep the unified ``codepilot chat --session <id>`` hint pointing at the right transcript.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_codex_sessions_root() -> Path:
    """Return the standard ``~/.codex/sessions`` directory."""
    return Path.home() / ".codex" / "sessions"


def codex_session_exists(
    project_path: str | Path,
    session_id: str,
    *,
    sessions_root: str | Path | None = None,
) -> bool:
    """Return True when Codex has a transcript whose id and cwd match ``project_path``."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    root = Path(sessions_root) if sessions_root is not None else default_codex_sessions_root()
    if not root.is_dir():
        return False
    target_cwd = _normalize_cwd(project_path)
    for candidate in root.glob(f"**/rollout-*-{sid}.jsonl"):
        meta = _read_session_meta(candidate)
        if not meta:
            continue
        if str(meta.get("id") or "").strip() != sid:
            continue
        if _normalize_cwd(meta.get("cwd") or "") == target_cwd:
            return True
    return False


def latest_codex_session_id(
    project_path: str | Path,
    *,
    sessions_root: str | Path | None = None,
    scan_limit: int = 200,
) -> str:
    """Return the most recently modified Codex session id whose cwd matches ``project_path``."""
    root = Path(sessions_root) if sessions_root is not None else default_codex_sessions_root()
    if not root.is_dir():
        return ""
    target_cwd = _normalize_cwd(project_path)
    inspected = 0
    for transcript in _walk_codex_sessions_recent_first(root):
        if scan_limit and inspected >= scan_limit:
            break
        inspected += 1
        meta = _read_session_meta(transcript)
        if not meta:
            continue
        if _normalize_cwd(meta.get("cwd") or "") != target_cwd:
            continue
        sid = str(meta.get("id") or "").strip()
        if sid:
            return sid
    return ""


def _walk_codex_sessions_recent_first(root: Path):
    """Yield ``rollout-*.jsonl`` files newest-first by walking year/month/day in reverse."""
    for year_dir in _sorted_subdirs(root, reverse=True):
        for month_dir in _sorted_subdirs(year_dir, reverse=True):
            for day_dir in _sorted_subdirs(month_dir, reverse=True):
                try:
                    files = [
                        item
                        for item in day_dir.iterdir()
                        if item.is_file()
                        and item.suffix == ".jsonl"
                        and item.name.startswith("rollout-")
                    ]
                except OSError:
                    continue
                files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
                yield from files


def _sorted_subdirs(parent: Path, *, reverse: bool = False) -> list[Path]:
    try:
        children = [item for item in parent.iterdir() if item.is_dir()]
    except OSError:
        return []
    children.sort(key=lambda item: item.name, reverse=reverse)
    return children


def _read_session_meta(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    if str(event.get("type") or "") != "session_meta":
        return None
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else None


def _normalize_cwd(value: str | Path) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        resolved = str(Path(text).resolve())
    except (OSError, ValueError):
        resolved = text
    return os.path.normcase(resolved)
