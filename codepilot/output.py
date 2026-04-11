"""Shared console output helpers."""

from __future__ import annotations

import sys

from rich.console import Console


def _console() -> Console:
    return Console(file=sys.stdout, highlight=False)


def echo(message: str = "", *, nl: bool = True, markup: bool = True) -> None:
    """Print user-facing output with optional Rich markup rendering."""
    _console().print(message or "", end="\n" if nl else "", markup=markup, highlight=False, soft_wrap=True)
