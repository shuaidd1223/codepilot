"""TODO/FIXME/XXX signal collector helpers."""

from __future__ import annotations

import re
import tokenize
from pathlib import Path

from codepilot.commands.inspect_signal_collectors_shared import (
    COMMENT_MARKERS_BY_EXT,
    SCAN_EXTS,
    should_skip_scan_path,
)

TODO_RE = re.compile(
    r"\b(?:TODO|FIXME)\b[:：]?\s*.{0,120}|(?<![.`\[\(])\bXXX\b(?:[:：]|\s+).{0,120}",
    re.IGNORECASE,
)
MD_TODO_RE = re.compile(
    r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)?(?:\[[ xX]\]\s*)?"
    r"(?:TODO|FIXME)\b[:：]?\s*.{0,120}|"
    r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)?(?:\[[ xX]\]\s*)?(?<![.`\[\(])XXX\b(?:[:：]|\s+).{0,120}",
    re.IGNORECASE,
)


def _find_unquoted_marker(line: str, markers: tuple[str, ...]) -> int:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        for marker in markers:
            if line.startswith(marker, index):
                return index
    return -1


def _todo_match_in_code_comment(path: Path, line: str) -> re.Match[str] | None:
    """Return TODO-like markers only when they appear in source comments."""
    match = TODO_RE.search(line)
    if not match:
        return None

    markers = COMMENT_MARKERS_BY_EXT.get(path.suffix)
    if not markers:
        return None
    todo_pos = match.start()
    marker_pos = _find_unquoted_marker(line, markers)
    return match if marker_pos != -1 and marker_pos <= todo_pos else None


def _todo_match_in_markdown(line: str, in_fenced_block: bool) -> re.Match[str] | None:
    """Keep documentation TODOs intentional and ignore prose/code examples."""
    if in_fenced_block:
        return None
    stripped = line.strip()
    if stripped.startswith("<!--") and "-->" in stripped:
        return TODO_RE.search(stripped)
    return MD_TODO_RE.search(stripped)


def _collect_python_todos(path: Path, project_path: Path, limit: int) -> list[str]:
    hits: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for token in tokenize.generate_tokens(fh.readline):
                if token.type != tokenize.COMMENT:
                    continue
                match = TODO_RE.search(token.string)
                if not match:
                    continue
                rel = path.relative_to(project_path)
                hits.append(f"{rel}:{token.start[0]}  {match.group(0).strip()}")
                if len(hits) >= limit:
                    break
    except Exception:
        return []
    return hits


def collect_todos(project_path: Path, limit: int = 20) -> str:
    hits: list[str] = []
    for path in project_path.rglob("*"):
        if len(hits) >= limit:
            break
        if not path.is_file() or path.suffix not in SCAN_EXTS or should_skip_scan_path(path):
            continue
        if path.suffix == ".py":
            hits.extend(_collect_python_todos(path, project_path, limit - len(hits)))
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                in_markdown_fence = False
                for lineno, line in enumerate(fh, 1):
                    if path.suffix == ".md":
                        stripped = line.lstrip()
                        if stripped.startswith("```") or stripped.startswith("~~~"):
                            in_markdown_fence = not in_markdown_fence
                            continue
                        match = _todo_match_in_markdown(line, in_markdown_fence)
                    else:
                        match = _todo_match_in_code_comment(path, line)
                    if match:
                        rel = path.relative_to(project_path)
                        hits.append(f"{rel}:{lineno}  {match.group(0).strip()}")
                        if len(hits) >= limit:
                            break
        except Exception:
            continue
    return "\n".join(hits) if hits else "（无）"
