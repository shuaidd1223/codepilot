from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands import setup as setup_cmd


def _init_test_env(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))


def _claude_action(payload: dict) -> dict:
    return next(
        item
        for item in payload["data"]["actions"]
        if item["kind"] == "claude_auto_install"
    )


def test_setup_skips_claude_auto_install_when_claude_exists(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    calls: list[list[str]] = []

    monkeypatch.setattr(
        setup_cmd,
        "_find_command",
        lambda name: str(tmp_path / "bin" / name) if name == "claude.cmd" else None,
    )
    monkeypatch.setattr(setup_cmd, "_run_command", lambda args: calls.append(args))

    data = setup_cmd.setup_project(project, dry_run=False, install_claude=True)

    action = next(
        item for item in data["actions"] if item["kind"] == "claude_auto_install"
    )
    assert action["status"] == "exists"
    assert action["command"] == []
    assert "claude.cmd" in action["detail"]
    assert calls == []


def test_setup_dry_run_reports_claude_npm_install_without_running_it(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(setup_cmd, "_find_command", lambda name: None)
    monkeypatch.setattr(
        setup_cmd,
        "_run_command",
        lambda args: (_ for _ in ()).throw(AssertionError("dry-run must not execute npm")),
    )

    result = CliRunner().invoke(main, ["setup", str(project), "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    action = _claude_action(json.loads(result.output))
    assert action["status"] == "would_install"
    assert action["command"] == ["npm", "install", "-g", "@anthropic-ai/claude-code"]


def test_setup_dry_run_includes_npm_registry_in_claude_install_command(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(setup_cmd, "_find_command", lambda name: None)

    result = CliRunner().invoke(
        main,
        [
            "setup",
            str(project),
            "--dry-run",
            "--json",
            "--npm-registry",
            "https://registry.npmmirror.com",
        ],
    )

    assert result.exit_code == 0, result.output
    action = _claude_action(json.loads(result.output))
    assert action["status"] == "would_install"
    assert action["command"] == [
        "npm",
        "install",
        "-g",
        "@anthropic-ai/claude-code",
        "--registry",
        "https://registry.npmmirror.com",
    ]


def test_setup_text_output_guides_claude_install_command(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(setup_cmd, "_find_command", lambda name: None)

    result = CliRunner().invoke(
        main,
        [
            "setup",
            str(project),
            "--dry-run",
            "--npm-registry",
            "https://registry.example",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        "npm install -g @anthropic-ai/claude-code --registry https://registry.example"
        in result.output
    )


def test_setup_claude_auto_install_reports_missing_npm(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(setup_cmd, "_find_command", lambda name: None)

    result = CliRunner().invoke(main, ["setup", str(project), "--install-claude", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "setup_error"
    assert "npm" in payload["error"]["message"]
    assert "@anthropic-ai/claude-code" in payload["error"]["message"]


def test_setup_claude_auto_install_reports_npm_failure(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()

    def fake_find(name: str) -> str | None:
        return str(tmp_path / "bin" / "npm.cmd") if name == "npm.cmd" else None

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, args, stderr="registry unavailable")

    monkeypatch.setattr(setup_cmd, "_find_command", fake_find)
    monkeypatch.setattr(setup_cmd, "_run_command", fake_run)

    result = CliRunner().invoke(
        main,
        [
            "setup",
            str(project),
            "--install-claude",
            "--npm-registry",
            "https://registry.example",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "Claude Code 安装失败" in payload["error"]["message"]
    assert "registry unavailable" in payload["error"]["message"]
