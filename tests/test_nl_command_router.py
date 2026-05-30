"""Tests for the remaining pending-choice helper."""

from __future__ import annotations

from codepilot.nl_command_router import CommandOption, pick_command_option


def test_pick_command_option_selects_numbered_pending_choice():
    options = [
        {"command": "use demo", "label": "在 demo 中继续"},
        {"command": "use docs", "label": "在 docs 中继续"},
    ]

    assert pick_command_option("2", options) == options[1]
    assert pick_command_option(" 1 ", options) == options[0]


def test_pick_command_option_accepts_dataclass_options_and_rejects_free_text():
    options = [CommandOption(command="workflow next demo auto", label="自动推进")]

    assert pick_command_option("1", options) == {
        "command": "workflow next demo auto",
        "label": "自动推进",
    }
    assert pick_command_option("查看状态", options) is None
    assert pick_command_option("2", options) is None
