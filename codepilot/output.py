"""Shared console output helpers."""

from __future__ import annotations

import sys

from rich.console import Console
from rich.markup import escape as _markup_escape


def _console() -> Console:
    return Console(file=sys.stdout, highlight=False)


def echo(message: str = "", *, nl: bool = True, markup: bool = True) -> None:
    """Print user-facing output with optional Rich markup rendering."""
    _console().print(message or "", end="\n" if nl else "", markup=markup, highlight=False, soft_wrap=True)


def safe(text) -> str:
    """Escape rich markup in untrusted text (agent output, exceptions, git stdout)."""
    return _markup_escape(str(text) if text is not None else "")
