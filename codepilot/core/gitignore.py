"""Helpers for maintaining project-local .gitignore entries."""

from __future__ import annotations

from pathlib import Path


def ensure_gitignore_entry(root: Path, entry: str, *, comment: str = "", dry_run: bool = False) -> str:
    """Ensure *entry* is listed in ``root/.gitignore``.

    When *comment* is provided and the entry is newly added, a comment line
    (prefixed with ``# ``) is written above the entry so users understand why
    the file was added.

    Returns one of ``exists``, ``created``, ``updated``, ``would_create``, or
    ``would_update``.
    """
    gitignore = root / ".gitignore"
    comment_line = f"# {comment}\n" if comment else ""

    if gitignore.exists():
        text = gitignore.read_text(encoding="utf-8", errors="replace")
        existing = {line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")}
        if entry in existing or f"/{entry}" in existing:
            return "exists"
        if dry_run:
            return "would_update"

        separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
        content = f"{text}{separator}{comment_line}{entry}\n"
        gitignore.write_text(content, encoding="utf-8")
        return "updated"

    if dry_run:
        return "would_create"

    gitignore.write_text(f"{comment_line}{entry}\n", encoding="utf-8")
    return "created"

