"""Console encoding bootstrap helpers."""

from __future__ import annotations

import os
import sys

_UTF8_CODE_PAGE = 65001


def _set_windows_console_codepage_utf8() -> None:
    """Switch Windows console input/output code pages to UTF-8 when possible."""
    if os.name != "nt":
        return
    try:
        import ctypes
    except Exception:
        return

    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(_UTF8_CODE_PAGE)
        kernel32.SetConsoleOutputCP(_UTF8_CODE_PAGE)
    except Exception:
        return


def _reconfigure_text_stream(stream, *, errors: str | None = None) -> None:
    """Best-effort UTF-8 reconfigure for stdio text streams."""
    if stream is None:
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return

    kwargs = {"encoding": "utf-8"}
    if errors is not None:
        kwargs["errors"] = errors
    try:
        reconfigure(**kwargs)
    except Exception:
        return


def configure_console_encoding() -> None:
    """Normalize Windows CLI stdio to UTF-8 to avoid mojibake."""
    if os.name != "nt":
        return

    _set_windows_console_codepage_utf8()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    _reconfigure_text_stream(getattr(sys, "stdin", None))
    _reconfigure_text_stream(getattr(sys, "stdout", None), errors="replace")
    _reconfigure_text_stream(getattr(sys, "stderr", None), errors="replace")
