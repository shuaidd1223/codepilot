from __future__ import annotations

import sys
from pathlib import Path

from codepilot.ai_support import providers
from codepilot.ai_support.cli_families import CLI_FAMILIES
from codepilot.ai_support.providers import resolve_cli_provider
from codepilot.binary_support.paths import executable_name


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("binary", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o755)
    return path


def _write_agents_toml(project: Path, family: str, command: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    project.joinpath("AGENTS.toml").write_text(
        "[agents.commands]\n"
        f'{family} = "{command.as_posix()}"\n',
        encoding="utf-8",
    )


def _set_frozen_binary(monkeypatch, binary_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(binary_path))


def _clear_command_env(monkeypatch, family: str) -> None:
    monkeypatch.delenv(CLI_FAMILIES[family].env_var, raising=False)
    monkeypatch.delenv("CODEPILOT_GLOBAL_CONFIG_PATH", raising=False)


def test_resolve_cli_provider_prefers_bundled_path_over_path_and_agents_commands(
    tmp_path,
    monkeypatch,
):
    _clear_command_env(monkeypatch, "codex")
    binary = _write_executable(tmp_path / executable_name("codepilot"))
    bundled = _write_executable(
        tmp_path / "bin" / "vendor" / executable_name("codex")
    )
    path_cmd = tmp_path / "path-bin" / executable_name("codex")
    custom_cmd = tmp_path / "custom-bin" / executable_name("codex")
    _write_agents_toml(tmp_path / "project", "codex", custom_cmd)
    _set_frozen_binary(monkeypatch, binary)
    monkeypatch.setattr(providers.shutil, "which", lambda cmd: str(path_cmd))

    resolved = resolve_cli_provider("codex", tmp_path / "project")

    assert Path(resolved.cmd) == bundled


def test_resolve_cli_provider_uses_path_before_agents_commands_when_bundled_missing(
    tmp_path,
    monkeypatch,
):
    _clear_command_env(monkeypatch, "opencode")
    binary = _write_executable(tmp_path / executable_name("codepilot"))
    custom_cmd = tmp_path / "custom-bin" / executable_name("opencode")
    _write_agents_toml(tmp_path / "project", "opencode", custom_cmd)
    _set_frozen_binary(monkeypatch, binary)
    monkeypatch.setattr(
        providers.shutil,
        "which",
        lambda cmd: str(tmp_path / "path-bin" / executable_name(cmd)),
    )

    resolved = resolve_cli_provider("opencode", tmp_path / "project")

    assert resolved.cmd == "opencode"


def test_resolve_cli_provider_falls_back_to_agents_commands_after_bundled_and_path(
    tmp_path,
    monkeypatch,
):
    _clear_command_env(monkeypatch, "codex")
    binary = _write_executable(tmp_path / executable_name("codepilot"))
    custom_cmd = tmp_path / "custom-bin" / executable_name("codex")
    _write_agents_toml(tmp_path / "project", "codex", custom_cmd)
    _set_frozen_binary(monkeypatch, binary)
    bundled_candidate = tmp_path / "bin" / "vendor" / executable_name("codex")
    bundled_candidate.mkdir(parents=True)
    monkeypatch.setattr(providers.shutil, "which", lambda cmd: None)

    resolved = resolve_cli_provider("codex", tmp_path / "project")

    assert Path(resolved.cmd) == custom_cmd
