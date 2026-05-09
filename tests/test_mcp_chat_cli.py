from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.chdir(project_path)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global.toml"))
    db.init_db()
    return project_path


def _capture_agent_launch(monkeypatch):
    from codepilot.commands import chat as chat_cmd

    calls: list[dict] = []

    def fake_launch(*, agent, project=None, prompt=""):
        calls.append({"agent": agent, "project": project, "prompt": prompt})
        return 0

    monkeypatch.setattr(chat_cmd, "_launch_mcp_agent_chat", fake_launch)
    return calls


def test_chat_agent_long_option_selects_agent(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _capture_agent_launch(monkeypatch)

    result = CliRunner().invoke(main, ["chat", "--agent", "claude"])

    assert result.exit_code == 0
    assert calls == [{"agent": "claude", "project": None, "prompt": ""}]


def test_chat_agent_short_option_selects_agent(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _capture_agent_launch(monkeypatch)

    result = CliRunner().invoke(main, ["chat", "-a", "codex"])

    assert result.exit_code == 0
    assert calls == [{"agent": "codex", "project": None, "prompt": ""}]


def test_chat_uses_automation_default_agent_from_config(tmp_path: Path, monkeypatch):
    project_path = _isolate(tmp_path, monkeypatch)
    (project_path / "AGENTS.toml").write_text(
        '[project]\nname = "demo"\n\n[automation]\ndefault_agent = "claude"\n',
        encoding="utf-8",
    )
    calls = _capture_agent_launch(monkeypatch)

    result = CliRunner().invoke(main, ["chat"])

    assert result.exit_code == 0
    assert calls == [{"agent": "claude", "project": None, "prompt": ""}]


def test_chat_defaults_to_opencode_without_config(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _capture_agent_launch(monkeypatch)

    result = CliRunner().invoke(main, ["chat"])

    assert result.exit_code == 0
    assert calls == [{"agent": "opencode", "project": None, "prompt": ""}]


def test_chat_rejects_unknown_agent(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _capture_agent_launch(monkeypatch)

    result = CliRunner().invoke(main, ["chat", "--agent", "ghost"])

    assert result.exit_code != 0
    assert calls == []
    assert "Invalid value for '--agent'" in result.output
