from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from codepilot import db
from codepilot.cli import main
from codepilot.commands import auto as auto_cmd
from codepilot.commands import auto_workflow as auto_workflow_mod


def _init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    db.init_db()


def test_resolve_project_for_prompt_strict_requires_registered_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    try:
        auto_cmd.resolve_project_for_prompt(
            auto_register=False,
            require_registered=True,
        )
    except click.ClickException as exc:
        message = exc.format_message()
    else:  # pragma: no cover - should always raise in this scenario
        raise AssertionError("expected ClickException for unregistered workspace")

    assert "codepilot init" in message


def test_resolve_project_for_prompt_returns_temporary_session_under_home(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    home = Path(tmp_path / "home")
    workdir = home / "scratch"
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workdir)

    project = auto_cmd.resolve_project_for_prompt(
        auto_register=False,
        allow_temporary=True,
    )

    assert project.get("is_temporary") is True
    assert "临时会话" in project.get("name", "")
    assert db.list_projects() == []


def test_chat_exit_under_home_uses_temporary_session_without_registering_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    home = Path(tmp_path / "home")
    workdir = home / "scratch"
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workdir)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="/exit\n")

    assert result.exit_code == 0
    assert "公共临时会话" in result.output
    assert db.list_projects() == []


def test_resolve_project_for_prompt_returns_temporary_session_under_system_temp(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    system_temp = tmp_path / "system-temp"
    workdir = system_temp / "scratch"
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(auto_workflow_mod.tempfile, "gettempdir", lambda: str(system_temp))
    monkeypatch.chdir(workdir)

    project = auto_cmd.resolve_project_for_prompt(
        auto_register=False,
        allow_temporary=True,
    )

    assert project.get("is_temporary") is True
    assert "临时会话" in project.get("name", "")
    assert db.list_projects() == []


def test_go_requires_registered_project_when_running_from_unregistered_workspace(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    home = Path(tmp_path / "home")
    workdir = home / "scratch"
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workdir)

    runner = CliRunner()
    result = runner.invoke(main, ["go", "帮我修复一个问题"])

    assert result.exit_code != 0
    assert "codepilot init" in result.output
