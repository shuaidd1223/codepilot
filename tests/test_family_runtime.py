from __future__ import annotations

import os
from pathlib import Path

import pytest

from codepilot.ai_support.family_runtime import (
    FamilyRuntimeUnavailable,
    build_env_for_family,
)
from codepilot.ai_support.cli_families import get_family
from codepilot.ai_support.opencode_runtime import build_opencode_env
from codepilot.commands import run as run_cmd
from codepilot.core.config import AgentsConfig


def _cfg(providers: dict[str, dict[str, str | bool | int]]) -> AgentsConfig:
    return AgentsConfig.from_dict({"providers": providers})


def test_cli_families_declare_env_bridge_config():
    claude = get_family("claude")
    codex = get_family("codex")
    opencode = get_family("opencode")

    assert claude is not None
    assert claude.env_bridge is not None
    assert claude.env_bridge.target_var == "ANTHROPIC_API_KEY"
    assert set(claude.env_bridge.source_providers) == {
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
    }

    assert codex is not None
    assert codex.env_bridge is not None
    assert codex.env_bridge.target_var == "OPENAI_API_KEY"
    assert set(codex.env_bridge.source_providers) == {
        "openai-gpt4o",
        "openai-gpt4",
        "openai-gpt35",
    }

    assert opencode is not None
    assert opencode.env_bridge is not None
    assert opencode.env_bridge.source_providers == "smart_pick"


def test_native_auth_file_takes_priority_over_config_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = _cfg({"claude-sonnet": {"api_key": "sk-should-not-inject"}})

    assert build_env_for_family("claude", cfg) == {}


def test_claude_and_codex_use_their_target_env_vars(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    claude_env = build_env_for_family(
        "claude",
        _cfg({"claude-sonnet": {"api_key": "sk-ant-test"}}),
    )
    codex_env = build_env_for_family(
        "codex",
        _cfg({"openai-gpt4o": {"api_key": "sk-oa-test"}}),
    )

    assert claude_env == {"ANTHROPIC_API_KEY": "sk-ant-test"}
    assert codex_env == {"OPENAI_API_KEY": "sk-oa-test"}


def test_opencode_uses_existing_smart_pick_order(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = _cfg(
        {
            "claude-sonnet": {"api_key": "sk-ant-test"},
            "deepseek": {
                "api_key": "sk-ds-test",
                "base_url": "https://api.deepseek.com",
            },
            "openai-gpt4o": {"api_key": "sk-oa-test"},
        }
    )

    env = build_env_for_family("opencode", cfg)

    assert env == {"ANTHROPIC_API_KEY": "sk-ant-test"}


def test_opencode_native_auth_file_suppresses_config_key_injection(tmp_path, monkeypatch):
    home = tmp_path / "home"
    auth_file = home / ".local" / "share" / "opencode" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = _cfg({"deepseek": {"api_key": "sk-ds-test", "base_url": "https://api.deepseek.com"}})

    assert build_env_for_family("opencode", cfg) == {}


def test_legacy_opencode_env_wrapper_matches_family_runtime_native_auth(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    auth_file = home / ".local" / "share" / "opencode" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = _cfg(
        {
            "deepseek": {
                "api_key": "sk-ds-test",
                "base_url": "https://api.deepseek.com",
            }
        }
    )

    assert build_opencode_env(cfg) == build_env_for_family("opencode", cfg)


def test_missing_key_without_native_auth_raises_clear_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    with pytest.raises(FamilyRuntimeUnavailable) as excinfo:
        build_env_for_family("codex", _cfg({}))

    message = str(excinfo.value)
    assert "codex" in message
    assert "OPENAI_API_KEY" in message
    assert "openai-gpt4o" in message


class _FakeCLIProvider:
    def __init__(self, exe: Path):
        self._exe = exe

    def find_executable(self) -> Path:
        return self._exe


def _write_builtin_phase_config(project_path: Path) -> None:
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[providers.claude-sonnet]
api_key = "sk-ant-test"

[providers.openai-gpt4o]
api_key = "sk-oa-test"

[providers.deepseek]
api_key = "sk-ds-test"
base_url = "https://api.deepseek.com"
""".strip(),
        encoding="utf-8",
    )


def _install_builtin_phase_fakes(monkeypatch, tmp_path: Path, captured: list[dict]):
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)

    def fake_resolve_cli_provider(provider_key, project_path=None):
        exe = tools / f"{provider_key}.cmd"
        exe.write_text("@echo off\n", encoding="utf-8")
        return _FakeCLIProvider(exe)

    def fake_run_command(cmd, **kwargs):
        captured.append(
            {
                "cmd": cmd,
                "env": {
                    "CODEPILOT_TEST_FAMILY": os.environ.get("CODEPILOT_TEST_FAMILY"),
                    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
                    "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
                },
            }
        )
        return 0, ""

    monkeypatch.setattr(
        run_cmd,
        "check_provider_availability",
        lambda agent, project_path=None: (True, f"ok:{agent}"),
    )
    monkeypatch.setattr(run_cmd, "resolve_cli_provider", fake_resolve_cli_provider)
    monkeypatch.setattr(run_cmd, "_run_command", fake_run_command)
    monkeypatch.setattr(run_cmd, "_read_output_file", lambda path: "")


@pytest.mark.parametrize("family", ["claude", "codex", "opencode"])
def test_builtin_phase_uses_family_runtime_env_for_all_families(tmp_path, monkeypatch, family):
    project_path = tmp_path / "project"
    project_path.mkdir()
    _write_builtin_phase_config(project_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEPILOT_TEST_FAMILY", raising=False)
    captured: list[dict] = []
    _install_builtin_phase_fakes(monkeypatch, tmp_path, captured)

    family_calls: list[str] = []

    def fake_build_env_for_family(family_name, cfg):
        family_calls.append(family_name)
        return {"CODEPILOT_TEST_FAMILY": family_name}

    monkeypatch.setattr(
        "codepilot.commands.run_builtin_executor.build_env_for_family",
        fake_build_env_for_family,
    )

    label, exit_code, _output = run_cmd._run_builtin_phase(
        task={"agent": family},
        project_path=project_path,
        phase="builder",
        prompt="请实现功能",
        output_path=project_path / f"{family}.txt",
        timeout=30,
        config_ref=project_path,
    )

    assert label == family
    assert exit_code == 0
    assert family_calls == [family]
    assert captured[0]["env"]["CODEPILOT_TEST_FAMILY"] == family
    assert os.environ.get("CODEPILOT_TEST_FAMILY") is None


def test_builtin_phase_native_auth_suppresses_api_key_override(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    _write_builtin_phase_config(project_path)
    home = tmp_path / "home"
    auth_file = home / ".claude" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured: list[dict] = []
    _install_builtin_phase_fakes(monkeypatch, tmp_path, captured)

    run_cmd._run_builtin_phase(
        task={"agent": "claude"},
        project_path=project_path,
        phase="builder",
        prompt="请实现功能",
        output_path=project_path / "claude.txt",
        timeout=30,
        config_ref=project_path,
    )

    assert captured[0]["env"]["ANTHROPIC_API_KEY"] is None


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        ("claude", "ANTHROPIC_API_KEY"),
        ("codex", "OPENAI_API_KEY"),
        ("opencode", "OpenCode"),
    ],
)
def test_builtin_phase_missing_key_fails_before_launch(tmp_path, monkeypatch, family, expected):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        "[project]\nname = \"demo\"\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    captured: list[dict] = []
    _install_builtin_phase_fakes(monkeypatch, tmp_path, captured)

    with pytest.raises(Exception) as excinfo:
        run_cmd._run_builtin_phase(
            task={"agent": family},
            project_path=project_path,
            phase="builder",
            prompt="请实现功能",
            output_path=project_path / f"{family}.txt",
            timeout=30,
            config_ref=project_path,
        )

    assert expected in str(excinfo.value)
    assert captured == []
