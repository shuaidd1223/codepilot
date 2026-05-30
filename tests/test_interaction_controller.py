"""Unit tests for the remaining workflow-session helper."""

from __future__ import annotations

from codepilot.ai_support.interaction_controller import WORKFLOW_PHASES, build_workflow_session_record


def test_workflow_session_record_is_workflow_only():
    assert "plan" in WORKFLOW_PHASES
    assert "question" not in WORKFLOW_PHASES

    assert build_workflow_session_record(phase="plan", intent="requirement", next_action="confirm") == {
        "phase": "plan",
        "intent": "requirement",
        "next_action": "confirm",
    }
