from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db
from pytest_mcp_chat_glob import expand_mcp_chat_test_globs


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

    def fake_launch(*, agent, project=None, prompt="", input_stream=None):
        calls.append({"agent": agent, "project": project, "prompt": prompt})
        return 0

    monkeypatch.setattr(chat_cmd, "_run_mcp_agent_chat_session", fake_launch)
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


def test_chat_blocks_windows_codex_interactive_tui_by_default(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _capture_agent_launch(monkeypatch)
    monkeypatch.setattr("codepilot.commands.chat.os.name", "nt")
    monkeypatch.delenv("CODEPILOT_ALLOW_WINDOWS_CODEX_TUI", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    result = CliRunner().invoke(main, ["chat", "-a", "codex"])

    assert result.exit_code != 0
    assert calls == []
    assert "Windows 下 Codex CLI 交互 TUI 当前不稳定" in result.output
    assert "codepilot chat -a opencode" in result.output


def test_chat_allows_windows_codex_interactive_tui_when_explicitly_requested(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _capture_agent_launch(monkeypatch)
    monkeypatch.setattr("codepilot.commands.chat.os.name", "nt")
    monkeypatch.setenv("CODEPILOT_ALLOW_WINDOWS_CODEX_TUI", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

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


def test_chat_prepare_uses_project_agent_language_for_opencode_profile(tmp_path: Path, monkeypatch):
    project_path = _isolate(tmp_path, monkeypatch)
    runtime_root = tmp_path / "codepilot-home"
    monkeypatch.setattr("codepilot.opencode.paths.global_storage_root", lambda: runtime_root)
    db.register_project("demo", str(project_path))
    (project_path / "AGENTS.toml").write_text(
        '[project]\nname = "demo"\n\n[automation]\nagent_language = "zh-CN"\n',
        encoding="utf-8",
    )

    from codepilot.commands import chat as chat_cmd

    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)

    launch = chat_cmd._prepare_mcp_agent_chat(agent="opencode", project=None, prompt="")

    config_path = Path(launch.env["OPENCODE_CONFIG"])
    assert config_path.is_relative_to(runtime_root)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["instructions"][0].endswith("codepilot.zh-CN.md")
    assert "任务状态" in payload["command"]


def test_chat_session_option_restores_codepilot_opencode_session(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from codepilot.commands import chat as chat_cmd

    calls: list[dict] = []

    def fake_launch(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(chat_cmd, "_run_mcp_agent_chat_session", fake_launch)

    result = CliRunner().invoke(main, ["chat", "--session", "ses_123"])

    assert result.exit_code == 0
    assert calls == [
        {
            "agent": "opencode",
            "project": None,
            "prompt": "",
            "session": "ses_123",
            "input_stream": None,
        }
    ]


def test_root_session_option_restores_codepilot_opencode_session(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from codepilot.commands import chat as chat_cmd

    calls: list[dict] = []

    def fake_launch(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(chat_cmd, "_run_mcp_agent_chat_session", fake_launch)

    result = CliRunner().invoke(main, ["--session", "ses_123"])

    assert result.exit_code == 0
    assert calls == [
        {
            "agent": "opencode",
            "project": None,
            "prompt": "",
            "session": "ses_123",
            "input_stream": None,
        }
    ]


def test_chat_ignores_removed_mcp_environment_toggle(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEPILOT_CHAT_MCP", "0")
    calls = _capture_agent_launch(monkeypatch)

    result = CliRunner().invoke(main, ["chat", "--agent", "opencode"])

    assert result.exit_code == 0
    assert calls == [{"agent": "opencode", "project": None, "prompt": ""}]


def test_chat_reports_missing_mcp_sdk_before_starting_tui(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    from codepilot.commands import chat as chat_cmd

    def missing_sdk() -> None:
        raise click.ClickException("CodePilot MCP 运行依赖未安装：缺少 Python 包 `mcp`。")

    def fail_run(*args, **kwargs):
        raise AssertionError("chat should fail before starting the TUI")

    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", missing_sdk)
    monkeypatch.setattr(chat_cmd.subprocess, "run", fail_run)

    result = CliRunner().invoke(main, ["chat", "--agent", "opencode"])

    assert result.exit_code != 0
    assert "CodePilot MCP 运行依赖未安装" in result.output


def test_chat_rejects_unknown_agent(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _capture_agent_launch(monkeypatch)

    result = CliRunner().invoke(main, ["chat", "--agent", "ghost"])

    assert result.exit_code != 0
    assert calls == []
    assert "Invalid value for '--agent'" in result.output


def test_chat_rejects_removed_no_mcp_option(tmp_path: Path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _capture_agent_launch(monkeypatch)

    result = CliRunner().invoke(main, ["chat", "--agent", "opencode", "--no-mcp"])

    assert result.exit_code != 0
    assert calls == []
    assert "No such option: --no-mcp" in result.output


def test_pytest_mcp_chat_glob_expands_phase5_verification_entrypoint():
    args = ["tests/test_mcp_chat_*", "-q"]

    assert expand_mcp_chat_test_globs(args, root=Path.cwd()) == [
        "tests/test_mcp_chat_agent_switch.py",
        "tests/test_mcp_chat_cli.py",
        "tests/test_mcp_chat_launchers.py",
        "tests/test_mcp_chat_lifecycle.py",
        "-q",
    ]


def test_pytest_scheduled_glob_expands_documented_verification_entrypoint():
    args = ["tests/test_scheduled_*", "-q"]
    expanded = expand_mcp_chat_test_globs(args, root=Path.cwd())

    assert "tests/test_scheduled_config.py" in expanded
    assert "tests/test_scheduled_examples_e2e.py" in expanded
    assert expanded[-1] == "-q"


def test_pytest_opencode_glob_expands_documented_verification_entrypoint():
    args = ["tests/test_opencode_*", "-q"]
    expanded = expand_mcp_chat_test_globs(args, root=Path.cwd())

    assert "tests/test_opencode_profile_config.py" in expanded
    assert expanded[-1] == "-q"
