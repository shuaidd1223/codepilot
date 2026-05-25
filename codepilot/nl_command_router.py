"""Small helpers for pending command choices.

Free-form chat text is handled by the configured agent. CodePilot keeps only the
numbered-choice helper used by card-driven workflows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CommandOption:
    command: str
    label: str


def _normalize_option(option: dict[str, Any] | CommandOption) -> dict[str, Any]:
    return asdict(option) if isinstance(option, CommandOption) else dict(option)


def pick_command_option(text: str, options: list[dict[str, Any]] | list[CommandOption]) -> dict[str, Any] | None:
    raw = " ".join(str(text or "").strip().split())
    if not raw or not raw.isdigit():
        return None
    index = int(raw) - 1
    normalized_options = [_normalize_option(option) for option in (options or [])]
    if index < 0 or index >= len(normalized_options):
        return None
    return normalized_options[index]
