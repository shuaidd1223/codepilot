"""Legacy live-output markdown normalization helpers."""

from __future__ import annotations

import re


_LIVE_HEAD_RE = re.compile(r"^\s*##\s+Live Output\s*$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_-]+)?\s*$")
_ROLE_MARK_RE = re.compile(r"^(user|codex|claude|assistant)$", re.IGNORECASE)


def _find_live_output_header(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if _LIVE_HEAD_RE.match(str(line or "").strip()):
            return i
    return -1


def _find_non_empty_line(lines: list[str], start_idx: int) -> int:
    idx = max(0, int(start_idx))
    while idx < len(lines) and not str(lines[idx] or "").strip():
        idx += 1
    return idx


def _parse_text_fence_open(line: str) -> str | None:
    open_m = _FENCE_RE.match(str(line or "").strip())
    if not open_m:
        return None
    lang = (open_m.group(2) or "").lower()
    if lang and lang != "text":
        return None
    return open_m.group(1)[0]


def _find_fence_close(lines: list[str], start_idx: int, *, fence_char: str) -> int:
    for i in range(start_idx + 1, len(lines)):
        m = _FENCE_RE.match(str(lines[i] or "").strip())
        if m and m.group(1)[0] == fence_char:
            return i
    return -1


def _legacy_live_marker(line: str) -> tuple[str, str] | None:
    trimmed = str(line or "").strip()
    if _ROLE_MARK_RE.match(trimmed):
        return ("role", trimmed[0].upper() + trimmed[1:].lower())
    if trimmed.lower() == "exec":
        return ("exec", "Exec")
    return None


def _has_legacy_live_markers(lines: list[str]) -> bool:
    return any(_legacy_live_marker(str(line or "")) for line in lines)


def _close_live_runtime_block(out: list[str], state: dict[str, object]) -> None:
    if not state["runtime_open"]:
        return
    out.extend(["~~~", ""])
    state["runtime_open"] = False


def _close_live_exec_block(out: list[str], state: dict[str, object]) -> None:
    if not state["exec_open"]:
        return
    out.extend(["~~~", ""])
    state["exec_open"] = False


def _open_live_role_section(out: list[str], state: dict[str, object], role_name: str) -> None:
    _close_live_runtime_block(out, state)
    _close_live_exec_block(out, state)
    out.extend([f"### {role_name}", ""])
    state["mode"] = role_name.lower()


def _open_live_exec_section(out: list[str], state: dict[str, object]) -> None:
    _close_live_runtime_block(out, state)
    _close_live_exec_block(out, state)
    out.extend(["### Exec", "", "~~~text"])
    state["exec_open"] = True
    state["mode"] = "exec"


def _open_live_runtime_section(out: list[str], state: dict[str, object]) -> None:
    if state["mode"] == "runtime" and state["runtime_open"]:
        return
    _close_live_exec_block(out, state)
    if not state["runtime_open"]:
        out.extend(["### Runtime", "", "~~~text"])
    state["runtime_open"] = True
    state["mode"] = "runtime"


def _normalize_legacy_live_inner_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    state: dict[str, object] = {"mode": "", "runtime_open": False, "exec_open": False}

    for line in lines:
        raw = str(line or "")
        marker = _legacy_live_marker(raw)
        if marker:
            marker_kind, marker_name = marker
            if marker_kind == "role":
                _open_live_role_section(out, state, marker_name)
                continue
            _open_live_exec_section(out, state)
            continue

        if not state["mode"]:
            _open_live_runtime_section(out, state)
        out.append(raw)

    _close_live_runtime_block(out, state)
    _close_live_exec_block(out, state)
    return out


def normalize_legacy_live_output_markdown(text: str) -> str:
    """Convert legacy ``## Live Output`` fenced text protocol to markdown sections.

    Older runs stored Codex/Claude stream protocol as one giant ``~~~text`` block:
    ``user`` / ``codex`` / ``exec`` markers remained plain text so the renderer
    couldn't style or parse markdown content inside that fence.
    """
    if not text:
        return text

    lines = text.splitlines()
    if not lines:
        return text

    head_idx = _find_live_output_header(lines)
    if head_idx < 0:
        return text

    body_start = _find_non_empty_line(lines, head_idx + 1)
    if body_start >= len(lines):
        return text

    fence_char = _parse_text_fence_open(str(lines[body_start] or ""))
    if not fence_char:
        return text

    body_end = _find_fence_close(lines, body_start, fence_char=fence_char)
    has_closing_fence = body_end >= 0
    if not has_closing_fence:
        body_end = len(lines)

    inner = lines[body_start + 1 : body_end]
    if not _has_legacy_live_markers(inner):
        return text

    out = lines[: head_idx + 1] + [""]
    out.extend(_normalize_legacy_live_inner_lines(inner))

    tail_start = body_end + 1 if has_closing_fence else body_end
    out.extend(lines[tail_start:])
    normalized = "\n".join(out).rstrip() + "\n"
    return normalized

