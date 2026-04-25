"""Shared console output helpers."""

from __future__ import annotations

import sys
from datetime import datetime

from rich.console import Console
from rich.markup import escape as _markup_escape

_CONSOLE: Console | None = None
_CONSOLE_STREAM = None
_LINE_OPEN = False
_LEVEL_ALIASES = {
    "debug": "debug",
    "trace": "debug",
    "info": "info",
    "ok": "success",
    "success": "success",
    "warn": "warning",
    "warning": "warning",
    "error": "error",
    "err": "error",
}
_LEVEL_STYLES = {
    "debug": ("DEBUG", "dim"),
    "info": ("INFO", "cyan"),
    "success": ("OK", "green"),
    "warning": ("WARN", "yellow"),
    "error": ("ERROR", "red"),
}


def _build_console(stream) -> Console:
    return Console(file=stream, highlight=False)


def _console() -> Console:
    global _CONSOLE, _CONSOLE_STREAM
    stream = sys.stdout
    if (
        _CONSOLE is None
        or _CONSOLE_STREAM is not stream
        or getattr(_CONSOLE_STREAM, "closed", False)
    ):
        _CONSOLE = _build_console(stream)
        _CONSOLE_STREAM = stream
    return _CONSOLE


def _now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _normalize_level(level: str | None) -> str:
    raw = str(level or "").strip().lower()
    return _LEVEL_ALIASES.get(raw, "info")


def _infer_level(message: str) -> str:
    lowered = message.lower()
    if "[red]" in lowered:
        return "error"
    if "[yellow]" in lowered:
        return "warning"
    return "info"


def _render_prefix(level: str, *, markup: bool) -> str:
    label, style = _LEVEL_STYLES[_normalize_level(level)]
    if not markup:
        return f"{_now_text()} {label:<5} "
    return f"[dim]{_now_text()}[/dim] [{style}]{label:<5}[/{style}] "


def _split_leading_newlines(message: str) -> tuple[str, str]:
    idx = 0
    while idx < len(message) and message[idx] == "\n":
        idx += 1
    return message[:idx], message[idx:]


def _format_echo_message(
    message: str,
    *,
    markup: bool,
    level: str | None = None,
    line_open: bool = False,
) -> str:
    if line_open or not message:
        return message
    leading, body = _split_leading_newlines(message)
    if not body:
        return message
    return f"{leading}{_render_prefix(level or _infer_level(body), markup=markup)}{body}"


def echo(
    message: str = "",
    *,
    nl: bool = True,
    markup: bool = True,
    level: str | None = None,
) -> None:
    """Print user-facing output with a unified timestamp + level prefix."""
    global _LINE_OPEN
    text = "" if message is None else str(message)
    rendered = _format_echo_message(text, markup=markup, level=level, line_open=_LINE_OPEN)
    _console().print(rendered or "", end="\n" if nl else "", markup=markup, highlight=False, soft_wrap=True)
    _LINE_OPEN = not nl


def safe(text) -> str:
    """Escape rich markup in untrusted text (agent output, exceptions, git stdout)."""
    return _markup_escape(str(text) if text is not None else "")
