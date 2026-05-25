"""Shared workflow-session record helpers for CLI/Web UI entrypoints."""

from __future__ import annotations


WORKFLOW_PHASES = ("intake", "plan", "command", "done", "error")


def build_workflow_session_record(
    *,
    phase: str,
    intent: str = "",
    next_action: str = "none",
) -> dict:
    """Build a standardized workflow-session record shared by entry points."""
    return {
        "phase": phase,
        "intent": intent or "",
        "next_action": next_action or "none",
    }
