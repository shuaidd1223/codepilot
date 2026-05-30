"""Small, auditable prompt template rendering for scheduled agents."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import is_dataclass, asdict
from typing import Any


PLACEHOLDER_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return None


def _resolve_path(context: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = context
    for part in path.split("."):
        mapping = _as_mapping(current)
        if mapping is not None:
            if part not in mapping:
                return False, None
            current = mapping[part]
            continue
        if hasattr(current, part):
            current = getattr(current, part)
            continue
        return False, None
    return True, current


def render_prompt_template(template: str, context: Mapping[str, Any]) -> str:
    """Render ``{{ field }}`` and ``{{ dotted.field }}`` without evaluating code.

    Unsupported expressions and missing values are replaced with explicit
    markers so the generated prompt remains auditable instead of failing at
    dispatch time. Filters, function calls, indexing and arithmetic are
    intentionally unsupported in this base module.
    """

    data = dict(context or {})

    def replace(match: re.Match[str]) -> str:
        raw_path = match.group(1).strip()
        if not PATH_RE.match(raw_path):
            return f"[[unsupported:{raw_path}]]"
        found, value = _resolve_path(data, raw_path)
        if not found or value is None:
            return f"[[missing:{raw_path}]]"
        return str(value)

    return PLACEHOLDER_RE.sub(replace, str(template or ""))
