from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.config import (
    GLOBAL_CONFIG_PATH_ENV,
    SECRETS_FILENAME,
    SECRETS_PATH_ENV,
    load_config,
    load_project_config,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _global_root(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".codepilot"


def _global_config_path(tmp_path: Path) -> Path:
    return _global_root(tmp_path) / "AGENTS.toml"


def _global_secrets_path(tmp_path: Path) -> Path:
    return _global_root(tmp_path) / SECRETS_FILENAME


def _project_config_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / name / "AGENTS.toml"


def _project_secrets_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / name / SECRETS_FILENAME


def test_load_project_config_merges_global_defaults_with_local_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv(SECRETS_PATH_ENV, raising=False)
    monkeypatch.delenv(GLOBAL_CONFIG_PATH_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    _write(
        _global_config_path(tmp_path),
        """
[automation]
planner = "claude"
task_agent = "codex"

[classifier]
provider = "openai-gpt4o"
timeout = 60

[providers.openai-gpt4o]
model = "gpt-5.4"
base_url = "https://global.example/v1"
""".strip(),
    )
    _write(
        _project_config_path(tmp_path, "proj"),
        """
[automation]
task_agent = "dual"

[classifier]
timeout = 15

[providers.openai-gpt4o]
model = "gpt-4o-mini"
""".strip(),
    )

    cfg = load_project_config(tmp_path / "proj")
    assert cfg is not None
    assert cfg.automation.planner == "claude"
    assert cfg.automation.task_agent == "dual"
    assert cfg.classifier.provider == "openai-gpt4o"
    assert cfg.classifier.timeout == 15
    assert cfg.providers["openai-gpt4o"].model == "gpt-4o-mini"
    assert cfg.providers["openai-gpt4o"].base_url == "https://global.example/v1"


def test_load_project_config_uses_global_when_project_file_missing(tmp_path, monkeypatch):
    monkeypatch.delenv(SECRETS_PATH_ENV, raising=False)
    monkeypatch.delenv(GLOBAL_CONFIG_PATH_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    project = tmp_path / "proj-no-local-config"
    project.mkdir(parents=True, exist_ok=True)
    _write(
        _global_config_path(tmp_path),
        """
[automation]
planner = "claude"
task_agent = "codex"
""".strip(),
    )

    cfg = load_project_config(project)
    assert cfg is not None
    assert cfg.automation.planner == "claude"
    assert cfg.automation.task_agent == "codex"
    assert cfg.project.name == "proj-no-local-config"


def test_project_secrets_override_global_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv(SECRETS_PATH_ENV, raising=False)
    monkeypatch.delenv(GLOBAL_CONFIG_PATH_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    _write(
        _global_config_path(tmp_path),
        """
[providers.openai-gpt4o]
model = "gpt-4o"
""".strip(),
    )
    _write(
        _global_secrets_path(tmp_path),
        """
[providers.openai-gpt4o]
api_key = "sk-global"
""".strip(),
    )
    _write(
        _project_config_path(tmp_path, "proj"),
        """
[providers.openai-gpt4o]
model = "gpt-4.1-mini"
""".strip(),
    )
    _write(
        _project_secrets_path(tmp_path, "proj"),
        """
[providers.openai-gpt4o]
api_key = "sk-local"
""".strip(),
    )

    cfg = load_project_config(tmp_path / "proj")
    assert cfg is not None
    assert cfg.providers["openai-gpt4o"].api_key == "sk-local"
    assert cfg.get_provider_api_key("openai-gpt4o") == "sk-local"


def test_load_config_without_local_file_falls_back_to_global(tmp_path, monkeypatch):
    monkeypatch.delenv(SECRETS_PATH_ENV, raising=False)
    monkeypatch.delenv(GLOBAL_CONFIG_PATH_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(sandbox)
    _write(
        _global_config_path(tmp_path),
        """
[automation]
planner = "claude"
""".strip(),
    )

    cfg = load_config()
    assert cfg is not None
    assert cfg.automation.planner == "claude"


def test_config_sync_global_writes_tool_root_config(tmp_path, monkeypatch):
    monkeypatch.delenv(SECRETS_PATH_ENV, raising=False)
    monkeypatch.delenv(GLOBAL_CONFIG_PATH_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))

    runner = CliRunner()
    result = runner.invoke(main, ["config", "sync", "--global"])
    assert result.exit_code == 0, result.output

    config_path = _global_config_path(tmp_path)
    assert config_path.exists()
    content = config_path.read_text(encoding="utf-8")
    assert "[project]" in content
    assert "[automation]" in content
    assert "[providers]" in content
