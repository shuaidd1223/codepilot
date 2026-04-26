"""Shared task-template validation helpers.

Used by both the CLI batch importer and the Web UI so task-template rules live
in exactly one place.
"""

from __future__ import annotations

import re
from typing import Iterable


REQUIRED_TASK_TEMPLATE_HEADINGS: tuple[dict[str, str], ...] = (
    {"label": "标题", "pattern": r"^\s*#\s+\S.*$", "flags": "m"},
    {"label": "Task Goal", "pattern": r"^\s*##\s+Task Goal\s*$", "flags": "mi"},
    {"label": "In Scope", "pattern": r"^\s*##\s+In Scope\s*$", "flags": "mi"},
    {"label": "Out of Scope", "pattern": r"^\s*##\s+Out of Scope\s*$", "flags": "mi"},
    {
        "label": "Forbidden",
        "pattern": r"^\s*##\s+Forbidden(?:\s+\(Hard Boundary\))?\s*$",
        "flags": "mi",
    },
    {"label": "Planning Evidence", "pattern": r"^\s*##\s+Planning Evidence\s*$", "flags": "mi"},
    {"label": "Acceptance Criteria", "pattern": r"^\s*##\s+Acceptance Criteria\s*$", "flags": "mi"},
    {"label": "Verification Matrix", "pattern": r"^\s*##\s+Verification Matrix\s*$", "flags": "mi"},
    {"label": "Reviewer Checkpoints", "pattern": r"^\s*##\s+Reviewer Checkpoints\s*$", "flags": "mi"},
)


def required_task_template_headings() -> list[dict[str, str]]:
    """Return JSON-serializable heading rules for frontend/backend reuse."""

    return [dict(item) for item in REQUIRED_TASK_TEMPLATE_HEADINGS]


def _regex_flags(flags_text: str) -> int:
    flags = 0
    if "i" in flags_text:
        flags |= re.IGNORECASE
    if "m" in flags_text:
        flags |= re.MULTILINE
    return flags


def missing_task_template_sections(content: str, *, headings: Iterable[dict[str, str]] | None = None) -> list[str]:
    """Return required task-template headings missing from markdown content."""

    rules = list(headings or REQUIRED_TASK_TEMPLATE_HEADINGS)
    text = str(content or "").strip()
    if not text:
        return [item["label"] for item in rules]

    missing: list[str] = []
    for item in rules:
        pattern = item.get("pattern") or ""
        if not pattern:
            continue
        flags = _regex_flags(item.get("flags", ""))
        if re.search(pattern, text, flags=flags) is None:
            missing.append(item.get("label") or pattern)
    return missing


def unreplaced_task_template_placeholders(
    content: str,
    *,
    placeholder_names: Iterable[str],
) -> list[str]:
    """Return raw ``{placeholder}`` tokens still present in a rendered task."""

    text = str(content or "")
    leftovers: list[str] = []
    for name in placeholder_names:
        normalized = str(name or "").strip()
        if not normalized:
            continue
        token = "{" + normalized + "}"
        if token in text:
            leftovers.append(token)
    return leftovers
