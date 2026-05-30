from __future__ import annotations

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands import chat as chat_cmd


def test_chat_entrypoint_uses_mcp_agent_without_local_intent_classifier(monkeypatch):
    launched: dict[str, object] = {}

    monkeypatch.setattr(chat_cmd, "_should_block_windows_codex_chat", lambda _agent: False)
    monkeypatch.setattr(chat_cmd, "_resolve_chat_agent", lambda agent, project: agent or "opencode")
    monkeypatch.setattr(
        chat_cmd,
        "_run_mcp_agent_chat_session",
        lambda **kwargs: launched.update(kwargs) or 0,
    )

    result = CliRunner().invoke(main, ["chat", "--project", "demo", "-a", "opencode"])

    assert result.exit_code == 0, result.output
    assert launched["agent"] == "opencode"
    assert launched["project"] == "demo"
    assert "input_stream" in launched
