from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main


def _init_test_env(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))


def test_init_adds_agents_toml_to_gitignore_once(tmp_path, monkeypatch):
    _init_test_env(tmp_path, monkeypatch)
    project = tmp_path / "demo"
    project.mkdir()
    gitignore = project / ".gitignore"
    gitignore.write_text("*.log", encoding="utf-8")

    runner = CliRunner()
    first = runner.invoke(main, ["init", str(project)])
    second = runner.invoke(main, ["init", str(project)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert (project / "AGENTS.toml").is_file()
    gitignore_lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert "*.log" in gitignore_lines
    assert gitignore_lines.count("AGENTS.toml") == 1

