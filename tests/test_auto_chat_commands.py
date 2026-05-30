"""Guards for the removed local chat command router."""

from __future__ import annotations

import importlib.util


def test_legacy_local_chat_command_modules_are_removed():
    assert importlib.util.find_spec("codepilot.commands.auto_chat") is None
    assert importlib.util.find_spec("codepilot.commands.auto_chat_commands") is None
