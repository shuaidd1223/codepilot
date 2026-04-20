from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands import doctor as doctor_mod
from codepilot.config import SECRETS_FILENAME, SECRETS_PATH_ENV


API_KEY_ENVS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HUNYUAN_API_KEY",
    "ZHIPU_API_KEY",
    "ERNIE_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_sources(monkeypatch, tmp_path):
    monkeypatch.delenv(SECRETS_PATH_ENV, raising=False)
    for env_var in API_KEY_ENVS:
        monkeypatch.delenv(env_var, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    return tmp_path


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _project(tmp_path: Path, monkeypatch, agents_toml: str, *, secrets: str = "") -> Path:
    project = tmp_path / "proj"
    _write(project / "AGENTS.toml", agents_toml)
    if secrets:
        _write(project / SECRETS_FILENAME, secrets)
    monkeypatch.chdir(project)
    return project


def _bucket(results: list[doctor_mod.CheckResult], name: str) -> doctor_mod.CheckResult:
    return next(item for item in results if item.name == name)


def test_check_api_keys_default_config_marks_missing_keys_optional(_isolate_sources, monkeypatch):
    _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents]
codex_cmd = "codex"
claude_cmd = "claude"

[automation]
planner = "codex"
""".strip(),
    )

    results = doctor_mod._check_api_keys()
    openai = _bucket(results, "api_key_openai_api_key")

    assert openai.ok is True
    assert openai.severity == "warning"
    assert "可选" in openai.detail
    assert not any(item.severity == "error" for item in results if item.name.startswith("api_key_"))


def test_check_api_keys_classifier_enabled_requires_its_provider_bucket(_isolate_sources, monkeypatch):
    _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents]
codex_cmd = "codex"
claude_cmd = "claude"

[automation]
planner = "codex"

[classifier]
enabled = true
provider = "openai-gpt4o"
""".strip(),
    )

    results = doctor_mod._check_api_keys()
    openai = _bucket(results, "api_key_openai_api_key")

    assert openai.ok is False
    assert openai.severity == "error"
    assert "当前配置必需" in openai.detail
    assert "openai-gpt4o" in openai.detail


def test_check_api_keys_disabled_classifier_does_not_require_provider_bucket(_isolate_sources, monkeypatch):
    _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents]
codex_cmd = "codex"
claude_cmd = "claude"

[automation]
planner = "codex"

[classifier]
enabled = false
provider = "openai-gpt4o"
""".strip(),
    )

    results = doctor_mod._check_api_keys()
    openai = _bucket(results, "api_key_openai_api_key")

    assert openai.ok is True
    assert openai.severity == "warning"
    assert "可选" in openai.detail


def test_check_api_keys_ignores_non_cli_planner_values_for_requiredness(_isolate_sources, monkeypatch):
    _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents]
codex_cmd = "codex"
claude_cmd = "claude"

[automation]
planner = "openai-gpt4o"

[inspect]
planner = "openai-gpt4o"
""".strip(),
    )

    results = doctor_mod._check_api_keys()
    openai = _bucket(results, "api_key_openai_api_key")

    assert openai.ok is True
    assert openai.severity == "warning"
    assert "当前配置必需" not in openai.detail


def test_check_api_keys_mixed_bucket_stays_non_green_when_only_one_provider_has_config_key(
    _isolate_sources,
    monkeypatch,
):
    _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents]
codex_cmd = "codex"
claude_cmd = "claude"

[automation]
planner = "codex"

[providers.openai-gpt4o]
model = "gpt-4o"
""".strip(),
        secrets="""
[providers.openai-gpt4o]
api_key = "sk-from-secrets"
""".strip(),
    )

    results = doctor_mod._check_api_keys()
    openai = _bucket(results, "api_key_openai_api_key")

    assert openai.ok is True
    assert openai.severity == "warning"
    assert "openai-gpt4o" in openai.detail
    assert "可选" in openai.detail
    assert "openai-gpt4" in openai.detail or "openai-gpt35" in openai.detail


def test_doctor_terminal_summary_separates_errors_and_warnings(monkeypatch):
    monkeypatch.setattr(
        doctor_mod,
        "run_all_checks",
        lambda: [
            doctor_mod.CheckResult("python_version", True, "Python 3.12"),
            doctor_mod.CheckResult(
                "api_key_openai_api_key",
                True,
                "OPENAI_API_KEY 未设置（当前为可选）",
                fix="export OPENAI_API_KEY=...",
                severity="warning",
            ),
            doctor_mod.CheckResult(
                "task_db",
                False,
                "任务数据库目录不可写",
                fix="mkdir -p ~/.codepilot",
                severity="error",
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "!  api_key_openai_api_key" in result.output
    assert "✘  task_db" in result.output
    assert "发现 1 个错误，1 个警告" in result.output
    assert "export OPENAI_API_KEY=..." in result.output


def test_doctor_json_ok_depends_only_on_error_severity(monkeypatch):
    monkeypatch.setattr(
        doctor_mod,
        "run_all_checks",
        lambda: [
            doctor_mod.CheckResult(
                "api_key_openai_api_key",
                True,
                "OPENAI_API_KEY 未设置（当前为可选）",
                fix="export OPENAI_API_KEY=...",
                severity="warning",
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["checks"][0]["severity"] == "warning"
