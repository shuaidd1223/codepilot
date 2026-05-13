"""Command-name helpers for AI-facing CodePilot docs."""

from __future__ import annotations

import sys
from pathlib import Path

def normalize_command_name(command_name: str | None = None) -> str:
    """Normalize a shell command label for published AI docs."""
    raw = (command_name or "").strip()
    if not raw:
        return "codepilot"

    if " " in raw:
        return raw

    candidate = Path(raw).name
    stem = Path(candidate).stem
    return stem or candidate or "codepilot"


def runtime_command_name() -> str:
    """Infer the command name currently used to invoke CodePilot."""
    argv0 = (sys.argv[0] or "").strip()
    if not argv0:
        return "codepilot"

    basename = Path(argv0).name.lower()
    if basename.startswith("pytest") or basename in {"-c", "-m"}:
        return "codepilot"
    if basename in {"python", "python.exe", "py", "py.exe", "__main__.py"}:
        return "python -m codepilot"

    if basename.endswith(".py"):
        return "python -m codepilot"

    return normalize_command_name(argv0)


def _cmd(command_name: str, suffix: str) -> str:
    command = normalize_command_name(command_name)
    return f"{command} {suffix}".strip()
