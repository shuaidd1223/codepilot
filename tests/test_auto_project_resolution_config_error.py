"""Tests for auto-fix config on ConfigError in project resolution.

Covers:
  - _apply_config_sync() removes legacy keys
  - _apply_config_sync() preserves clean configs
  - _auto_fix_config_error() in non-interactive mode
  - _resolve_from_config_strategy() auto-fixes ConfigError on user confirm
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.core.config import ConfigError


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _read_toml(path: Path) -> dict:
    import tomllib

    return tomllib.loads(path.read_text(encoding="utf-8"))


# ── _apply_config_sync ──────────────────────────────────────────────────


def test_apply_config_sync_removes_legacy_keys(tmp_path):
    """Legacy codex_cmd / claude_cmd should be removed after sync."""
    config_path = tmp_path / "AGENTS.toml"
    _write(
        config_path,
        """
[project]
name = "test-project"

[agents]
planner = "claude"
codex_cmd = "codex-custom"
claude_cmd = "claude-custom"

[automation]
planner = "claude"
""".strip(),
    )

    from codepilot.commands.auto_project_resolution import _apply_config_sync

    assert _apply_config_sync(config_path) is True

    parsed = _read_toml(config_path)
    agents = parsed.get("agents", {})
    assert "codex_cmd" not in agents
    assert "claude_cmd" not in agents
    assert agents.get("planner") == "claude"
    # commands mapping should be present (config sync adds default commands)
    assert "commands" in agents
    assert parsed["automation"]["planner"] == "claude"


def test_apply_config_sync_preserves_valid_config(tmp_path):
    """A clean config should remain functional after sync."""
    config_path = tmp_path / "AGENTS.toml"
    _write(
        config_path,
        """
[project]
name = "test-project"
base_branch = "main"

[agents]
planner = "codex"

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "codex"
task_agent = "dual"
executor = "builtin"
""".strip(),
    )

    from codepilot.commands.auto_project_resolution import _apply_config_sync

    assert _apply_config_sync(config_path) is True

    parsed = _read_toml(config_path)
    assert parsed["project"]["name"] == "test-project"
    assert parsed["project"]["base_branch"] == "main"
    assert parsed["agents"]["planner"] == "codex"
    assert parsed["agents"]["commands"]["codex"] == "codex"
    assert parsed["automation"]["planner"] == "codex"
    assert parsed["automation"]["task_agent"] == "dual"


def test_apply_config_sync_fixes_missing_sections(tmp_path):
    """An AGENTS.toml with only partial sections should get all defaults."""
    config_path = tmp_path / "AGENTS.toml"
    _write(
        config_path,
        """
[project]
name = "minimal"
""".strip(),
    )

    from codepilot.commands.auto_project_resolution import _apply_config_sync

    assert _apply_config_sync(config_path) is True

    parsed = _read_toml(config_path)
    assert parsed["project"]["name"] == "minimal"
    # automation section should be added with defaults
    assert "automation" in parsed
    assert parsed["automation"]["planner"] == "codex"
    assert parsed["automation"]["executor"] == "builtin"
    # agents section should be added
    assert "agents" in parsed
    assert "commands" in parsed["agents"]


# ── _auto_fix_config_error (non-interactive) ────────────────────────────


def _patch_stdin_non_interactive(monkeypatch):
    """Monkeypatch click stdin stream so isatty() returns False."""
    import io

    class _FakeStdin(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr(
        "click.get_text_stream",
        lambda name: _FakeStdin() if name == "stdin" else io.StringIO(),
    )


def test_auto_fix_config_error_non_interactive_returns_false(tmp_path, monkeypatch):
    """In non-interactive mode (stdin is not a TTY), should return False."""
    _patch_stdin_non_interactive(monkeypatch)

    from codepilot.commands.auto_project_resolution import _auto_fix_config_error

    config_path = tmp_path / "AGENTS.toml"
    _write(config_path, "[project]\nname = 'test'\n")

    error = ConfigError("AGENTS.toml 含已废弃字段：codex_cmd")
    result = _auto_fix_config_error(config_path, error)
    assert result is False


def test_auto_fix_config_error_non_interactive_hints_manual_command(tmp_path, monkeypatch, capsys):
    """Should tell the user to run 'codepilot config sync' in non-interactive mode."""
    _patch_stdin_non_interactive(monkeypatch)

    from codepilot.commands.auto_project_resolution import _auto_fix_config_error

    config_path = tmp_path / "AGENTS.toml"
    _write(config_path, "[project]\nname = 'test'\n")

    error = ConfigError("AGENTS.toml 含已废弃字段：codex_cmd")
    _auto_fix_config_error(config_path, error)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "codepilot config sync" in output or "config sync" in output


# ── _resolve_from_config_strategy integration via CliRunner ─────────────


def _patch_stdin_interactive(monkeypatch):
    """Monkeypatch click stdin stream so isatty() returns True."""
    import io

    class _InteractiveStdin(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(
        "click.get_text_stream",
        lambda name: _InteractiveStdin() if name == "stdin" else io.StringIO(),
    )


def test_resolve_from_config_error_decline_fix(tmp_path, monkeypatch):
    """When user declines auto-fix, the ConfigError should propagate."""
    import click

    _patch_stdin_interactive(monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    from codepilot.storage import database as db

    db.init_db()

    project = tmp_path / "geo"
    project.mkdir()
    config_path = project / "AGENTS.toml"
    _write(
        config_path,
        """
[project]
name = "geo"

[agents]
codex_cmd = "codex-custom"
claude_cmd = "claude-custom"

[automation]
planner = "codex"
""".strip(),
    )

    from codepilot.commands.auto_project_resolution import resolve_project_for_prompt

    # Simulate answering 'n' to decline the auto-fix
    monkeypatch.setattr("click.confirm", lambda *a, **kw: False)

    with pytest.raises(click.ClickException) as excinfo:
        resolve_project_for_prompt(
            project=None,
            cwd=project,
            auto_register=False,
            require_registered=True,
        )
    assert "废弃字段" in str(excinfo.value) or "codex_cmd" in str(excinfo.value)
    # Config should NOT have been fixed
    parsed = _read_toml(config_path)
    agents = parsed.get("agents", {})
    assert "codex_cmd" in agents


def test_resolve_from_config_error_accept_fix(tmp_path, monkeypatch):
    """When user accepts auto-fix, config should be synced."""
    _patch_stdin_interactive(monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    from codepilot.storage import database as db

    db.init_db()

    project = tmp_path / "geo"
    project.mkdir()
    config_path = project / "AGENTS.toml"
    _write(
        config_path,
        """
[project]
name = "geo"

[agents]
codex_cmd = "codex-custom"

[automation]
planner = "codex"
""".strip(),
    )
    # Register project so resolution succeeds after fix
    db.register_project("geo", str(project), config_file=str(config_path))

    from codepilot.commands.auto_project_resolution import resolve_project_for_prompt

    # Simulate answering 'Y' to accept the auto-fix
    monkeypatch.setattr("click.confirm", lambda *a, **kw: True)

    result = resolve_project_for_prompt(
        project=None,
        cwd=project,
        auto_register=False,
        require_registered=True,
    )
    # Resolution should succeed
    assert result is not None
    assert result["name"] == "geo"
    # Config should have been fixed (legacy key removed)
    parsed = _read_toml(config_path)
    agents = parsed.get("agents", {})
    assert "codex_cmd" not in agents, f"Legacy key should be removed: {parsed}"
    assert "commands" in agents


def test_resolve_project_for_prompt_non_interactive_legacy_keys(tmp_path, monkeypatch):
    """Non-interactive resolve_project_for_prompt should raise ClickException."""
    import click

    _patch_stdin_non_interactive(monkeypatch)
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    from codepilot.storage import database as db

    db.init_db()

    project = tmp_path / "geo"
    project.mkdir()
    _write(
        project / "AGENTS.toml",
        """
[project]
name = "geo"

[agents]
codex_cmd = "old-cmd"

[automation]
planner = "codex"
""".strip(),
    )

    from codepilot.commands.auto_project_resolution import resolve_project_for_prompt

    with pytest.raises(click.ClickException) as excinfo:
        resolve_project_for_prompt(
            project=None,
            cwd=project,
            auto_register=False,
            require_registered=True,
        )
    assert "废弃字段" in str(excinfo.value) or "codex_cmd" in str(excinfo.value)
