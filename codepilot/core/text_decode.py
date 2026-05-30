"""Robust text decoding helpers for subprocess output on mixed-encoding systems."""

from __future__ import annotations

import codecs

_SUBPROCESS_TEXT_ENCODINGS: tuple[str, ...] = (
    "utf-8",
    "utf-8-sig",
    "gb18030",
    "cp936",
)

_UTF16_BOMS: tuple[bytes, ...] = (
    codecs.BOM_UTF16_LE,
    codecs.BOM_UTF16_BE,
)


def decode_subprocess_text(chunk: str | bytes | None) -> str:
    """Decode one subprocess output chunk with pragmatic Windows fallbacks.

    On Windows some CLI tools emit UTF-16 LE output.  A leading BOM is
    detected first so that the correct codec is chosen immediately; for
    chunks without a BOM the full encoding cascade is tried.
    """
    if not chunk:
        return ""
    if isinstance(chunk, str):
        return chunk
    # Fast path: leading UTF-16 BOM → decode with utf-16 (handles LE/BE)
    if chunk.startswith(_UTF16_BOMS):
        return chunk.decode("utf-16")
    for encoding in _SUBPROCESS_TEXT_ENCODINGS:
        try:
            return chunk.decode(encoding)
        except UnicodeDecodeError:
            continue
    return chunk.decode("utf-8", errors="replace")

