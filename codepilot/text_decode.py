"""Robust text decoding helpers for subprocess output on mixed-encoding systems."""

from __future__ import annotations


_SUBPROCESS_TEXT_ENCODINGS: tuple[str, ...] = (
    "utf-8",
    "utf-8-sig",
    "gb18030",
    "cp936",
)


def decode_subprocess_text(chunk: str | bytes | None) -> str:
    """Decode one subprocess output chunk with pragmatic Windows fallbacks."""
    if not chunk:
        return ""
    if isinstance(chunk, str):
        return chunk
    for encoding in _SUBPROCESS_TEXT_ENCODINGS:
        try:
            return chunk.decode(encoding)
        except UnicodeDecodeError:
            continue
    return chunk.decode("utf-8", errors="replace")

