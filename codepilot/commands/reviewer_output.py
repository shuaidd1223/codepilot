"""Reviewer output parsing.

The reviewer phase is instructed to append a fenced JSON block describing
its verdict, blockers and advisory notes after the human-readable analysis.
This module provides one shared parser that:

1. Prefers the JSON block (fast, typed, unambiguous).
2. Falls back to the legacy ``VERDICT: PASS|FAIL`` + ``需要修复的点``
   regex strategy when the JSON is missing or malformed.

Consumers get a ``ReviewerVerdict`` dataclass; legacy string-only callers
still work via the thin helpers exposed on top of it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional


REVIEWER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "ac_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": ["PASS", "FAIL", "N/A"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "status"],
                "additionalProperties": False,
            },
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Actionable fix instructions. Non-empty iff verdict=fail.",
        },
        "advisory": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Non-blocking observations; NEVER flip verdict to fail.",
        },
    },
    "required": ["verdict"],
    "additionalProperties": False,
}


@dataclass
class ReviewerVerdict:
    """Normalized reviewer output consumed by executor + webui."""

    verdict: str = "unknown"  # "pass" | "fail" | "unknown"
    blockers: list[str] = field(default_factory=list)
    advisory: list[str] = field(default_factory=list)
    ac_checks: list[dict] = field(default_factory=list)
    source: str = "unknown"  # "json" | "legacy" | "empty"
    raw_json: Optional[dict] = None


_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n(?P<body>\{.*?\})\s*\n```",
    re.DOTALL | re.IGNORECASE,
)
_NEEDS_FIX_HEADER_RE = re.compile(
    r"(?mi)^\s*(需要修复的点|需要修复|需要处理|修复建议)\s*[:：]?\s*$"
)
_NEEDS_FIX_BLOCK_RE = re.compile(
    r"(?:需要修复的点|需要修复|需要处理|修复建议)\s*[:：]?\s*\n(.*?)(?:\n\s*VERDICT\s*:|$)",
    re.DOTALL | re.IGNORECASE,
)
_VERDICT_LINE_RE = re.compile(r"VERDICT\s*:\s*(PASS|FAIL)\b", re.IGNORECASE)
_AC_LINE_RE = re.compile(
    r"(?m)^\s*AC\s*#\d+\s*[:：]\s*(PASS|FAIL|N/A)", re.IGNORECASE
)
_VERDICT_STRIPPER_RE = re.compile(r"\s*VERDICT\s*:", re.IGNORECASE)
_REVIEW_COMMENTS_HEADER_RE = re.compile(
    r"(?mi)^\s*(?:Full\s+review\s+comments|Review\s+comments)\s*[:：]?\s*$"
)
_PRIORITY_FINDING_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(P[123])\]\s*(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_BLOCKING_PRIORITIES = {"P1", "P2"}


def _extract_trailing_json(text: str) -> Optional[dict]:
    """Pick the *last* fenced JSON object in the transcript, if any."""
    matches = list(_JSON_FENCE_RE.finditer(text))
    for match in reversed(matches):
        body = match.group("body").strip()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _coerce_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _coerce_ac_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().upper()
        if status not in {"PASS", "FAIL", "N/A"}:
            continue
        out.append(
            {
                "id": str(item.get("id") or "").strip(),
                "status": status,
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return out


def _priority_findings(text: str) -> list[dict[str, str]]:
    """Extract Codex-style ``[P1]`` / ``[P2]`` / ``[P3]`` review findings."""
    lines = str(text or "").splitlines()
    findings: list[dict[str, str]] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        match = _PRIORITY_FINDING_LINE_RE.match(raw)
        if not match:
            index += 1
            continue
        priority = match.group(1).upper()
        block = [f"[{priority}] {match.group('body').strip()}"]
        index += 1
        while index < len(lines):
            next_line = lines[index]
            if _PRIORITY_FINDING_LINE_RE.match(next_line):
                break
            if not next_line.strip():
                break
            if re.match(r"^\s{2,}\S", next_line) or next_line.startswith("\t"):
                block.append(next_line.strip())
                index += 1
                continue
            break
        findings.append({"priority": priority, "text": "\n".join(block).strip()})
    return findings


def _priority_finding_texts(text: str) -> list[str]:
    return [item["text"] for item in _priority_findings(text) if item.get("text")]


def _has_blocking_priority_finding(text: str) -> bool:
    return any(item["priority"] in _BLOCKING_PRIORITIES for item in _priority_findings(text))


def _has_review_comments_body(text: str) -> bool:
    match = _REVIEW_COMMENTS_HEADER_RE.search(text or "")
    if not match:
        return False
    body = str(text or "")[match.end():].strip()
    if not body:
        return False
    lowered = body.lower()
    return not any(marker in lowered for marker in ("no findings", "no issues", "nothing to report"))


def _parse_from_json(payload: dict) -> Optional[ReviewerVerdict]:
    """Hydrate ReviewerVerdict from a JSON payload. Returns None if invalid."""
    verdict_raw = str(payload.get("verdict") or "").strip().lower()
    if verdict_raw not in {"pass", "fail"}:
        return None
    blockers = _coerce_str_list(payload.get("blockers"))
    advisory = _coerce_str_list(payload.get("advisory"))
    ac_checks = _coerce_ac_list(payload.get("ac_checks"))

    # If verdict=fail but the reviewer forgot to enumerate blockers, we still
    # accept it — the regex fallback may scavenge a prose block. Don't silently
    # flip verdict.
    return ReviewerVerdict(
        verdict=verdict_raw,
        blockers=blockers,
        advisory=advisory,
        ac_checks=ac_checks,
        source="json",
        raw_json=payload,
    )


def _legacy_verdict(text: str) -> str:
    """Reproduces the pre-JSON-era verdict heuristic (kept for compatibility)."""
    ac_lines = _AC_LINE_RE.findall(text)
    has_failed_ac = any(v.lower() == "fail" for v in ac_lines)
    has_needs_fix = bool(_NEEDS_FIX_HEADER_RE.search(text))
    has_blocking_finding = _has_blocking_priority_finding(text)
    has_priority_finding = bool(_priority_findings(text))

    for line in reversed(text.splitlines()):
        match = _VERDICT_LINE_RE.search(line)
        if match:
            explicit = match.group(1).lower()
            if explicit == "fail":
                return "fail"
            if has_failed_ac or has_needs_fix or has_blocking_finding:
                return "fail"
            return "pass"

    if not text.strip():
        return "unknown"

    if has_failed_ac or has_needs_fix or has_blocking_finding:
        return "fail"

    if has_priority_finding or _has_review_comments_body(text):
        return "unknown"

    if ac_lines and all(v.lower() in {"pass", "n/a"} for v in ac_lines):
        return "pass"

    return "pass"


def _legacy_findings(text: str, *, cap: int = 2000) -> str:
    """Reproduce the pre-JSON-era findings extraction for backward compat."""
    if not text:
        return ""
    stripped = text.strip()

    match = _NEEDS_FIX_BLOCK_RE.search(stripped)
    if match:
        block = match.group(1).strip()
        if block:
            return block

    priority_findings = _priority_finding_texts(stripped)
    if priority_findings:
        out = "\n\n".join(priority_findings).strip()
        if len(out) > cap:
            out = out[-cap:]
        return out

    lines = [ln for ln in stripped.splitlines() if not _VERDICT_STRIPPER_RE.match(ln)]
    out = "\n".join(lines).strip()
    if len(out) > cap:
        out = out[-cap:]
    return out


def parse_reviewer_output(review_output: str) -> ReviewerVerdict:
    """Parse a reviewer transcript into a normalized verdict model.

    Strategy:
    - Prefer a trailing ```json``` fence; if present and contains a valid
      ``verdict`` field, use it as-is (blockers/advisory/ac_checks included).
    - Otherwise fall back to the legacy ``VERDICT:`` line + ``需要修复的点``
      section heuristic so reviewers that haven't adopted the JSON fence
      keep working.
    """
    text = review_output or ""
    if not text.strip():
        return ReviewerVerdict(verdict="unknown", source="empty")

    payload = _extract_trailing_json(text)
    if payload is not None:
        parsed = _parse_from_json(payload)
        if parsed is not None:
            # Augment missing blockers with the legacy prose block when the
            # reviewer only gave us a verdict in JSON but kept the fix list
            # up in the human-readable body.
            if parsed.verdict == "fail" and not parsed.blockers:
                legacy_findings = _legacy_findings(text)
                if legacy_findings:
                    parsed.blockers = [legacy_findings]
            return parsed

    verdict = _legacy_verdict(text)
    priority_findings = _priority_finding_texts(text)
    findings = _legacy_findings(text)
    blockers = priority_findings if priority_findings and verdict in {"fail", "unknown"} else []
    if not blockers and verdict == "fail" and findings:
        blockers = [findings]
    return ReviewerVerdict(
        verdict=verdict,
        blockers=blockers,
        source="legacy",
    )


def format_findings_for_builder(verdict_model: ReviewerVerdict) -> str:
    """Stringify blockers as bullet list for the next builder round input."""
    if not verdict_model.blockers:
        return ""
    bullets: list[str] = []
    for item in verdict_model.blockers:
        line = item.strip()
        if not line:
            continue
        if line.startswith(("- ", "* ")):
            bullets.append(line)
        else:
            bullets.append(f"- {line}")
    return "\n".join(bullets)
