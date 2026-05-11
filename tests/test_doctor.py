from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands import doctor as doctor_mod
from codepilot.core.config import SECRETS_FILENAME, SECRETS_PATH_ENV
from codepilot.storage import database as db


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


def _bucket_or_none(results: list[doctor_mod.CheckResult], name: str) -> doctor_mod.CheckResult | None:
    return next((item for item in results if item.name == name), None)


def test_check_api_keys_default_config_silently_skips_optional_missing_keys(_isolate_sources, monkeypatch):
    _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "codex"
""".strip(),
    )

    results = doctor_mod._check_api_keys()

    assert _bucket_or_none(results, "api_key_openai_api_key") is None
    assert not any(item.severity == "error" for item in results if item.name.startswith("api_key_"))


def test_check_api_keys_classifier_enabled_requires_its_provider_bucket(_isolate_sources, monkeypatch):
    _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents.commands]
codex = "codex"
claude = "claude"

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

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "codex"

[classifier]
enabled = false
provider = "openai-gpt4o"
""".strip(),
    )

    results = doctor_mod._check_api_keys()

    assert _bucket_or_none(results, "api_key_openai_api_key") is None


def test_check_api_keys_ignores_non_cli_planner_values_for_requiredness(_isolate_sources, monkeypatch):
    _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "openai-gpt4o"

[inspect]
planner = "openai-gpt4o"
""".strip(),
    )

    results = doctor_mod._check_api_keys()

    assert _bucket_or_none(results, "api_key_openai_api_key") is None


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

[agents.commands]
codex = "codex"
claude = "claude"

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
    openai = _bucket_or_none(results, "api_key_openai_api_key")

    assert openai is None


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
    assert payload["command"] == "doctor"
    assert payload["data"]["status_emoji"] == "✓"
    assert payload["data"]["checks"][0]["severity"] == "warning"


def test_doctor_subcommand_json_outputs_json(monkeypatch):
    monkeypatch.setattr(
        doctor_mod,
        "run_all_checks",
        lambda: [doctor_mod.CheckResult("python_version", True, "Python 3.12")],
    )

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "doctor"
    assert payload["data"]["status_emoji"] == "✓"
    assert payload["data"]["checks"][0]["name"] == "python_version"


def test_doctor_help_shows_subcommand_json_option():
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "--json" in result.output


@pytest.mark.parametrize(
    ("results", "expected_emoji"),
    [
        ([doctor_mod.CheckResult("python_version", True, "Python 3.12")], "✓"),
        (
            [
                doctor_mod.CheckResult(
                    "api_key_openai_api_key",
                    True,
                    "OPENAI_API_KEY 未设置（当前为可选）",
                    severity="warning",
                ),
            ],
            "✓",
        ),
        (
            [
                doctor_mod.CheckResult(
                    "task_db",
                    False,
                    "任务数据库目录不可写",
                    severity="error",
                ),
            ],
            "✘",
        ),
    ],
)
def test_doctor_json_status_emoji_depends_on_error_severity(
    monkeypatch,
    results,
    expected_emoji,
):
    monkeypatch.setattr(doctor_mod, "run_all_checks", lambda: results)

    runner = CliRunner()
    result = runner.invoke(main, ["--json", "doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is (expected_emoji != "✘")
    assert payload["command"] == "doctor"
    assert payload["data"]["status_emoji"] == expected_emoji


def _register_project(tmp_path: Path, monkeypatch, agents_toml: str) -> dict:
    project = _project(tmp_path, monkeypatch, agents_toml)
    db.init_db()
    return db.register_project("demo", str(project), config_file=str(project / "AGENTS.toml"))


def test_doctor_project_json_includes_project_and_service_checks(_isolate_sources, monkeypatch):
    _register_project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "codex"
""".strip(),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--project", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "doctor"
    assert payload["data"]["project"]["name"] == "demo"
    check_names = {item["name"] for item in payload["data"]["checks"]}
    assert "project_config" in check_names
    assert "service_daemon" in check_names
    assert "service_webui" in check_names


def test_doctor_services_json_reports_stale_service_state(_isolate_sources, monkeypatch):
    _register_project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "codex"
""".strip(),
    )
    db.upsert_service_state(
        "daemon",
        "demo",
        pid=999999,
        status="running",
        log_path="daemon.log",
        meta={"project": "demo"},
    )

    result = CliRunner().invoke(main, ["doctor", "--services", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    stale = next(item for item in payload["data"]["checks"] if item["name"] == "service_daemon")
    assert stale["severity"] == "warning"
    assert "stale" in stale["detail"].lower()


def test_doctor_warns_when_feishu_enabled_without_secret(_isolate_sources, monkeypatch):
    _register_project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "codex"

[feishu_bot]
enabled = true
app_id = "cli_xxx"
""".strip(),
    )

    result = CliRunner().invoke(main, ["doctor", "--project", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    feishu = next(item for item in payload["data"]["checks"] if item["name"] == "feishu_config")
    assert feishu["severity"] == "warning"
    assert "app_secret" in feishu["detail"]
    assert "cli_xxx" not in json.dumps(payload, ensure_ascii=False)


def test_doctor_fix_runs_project_setup_without_touching_codex_hooks(_isolate_sources, monkeypatch):
    project = _isolate_sources / "fix-project"
    project.mkdir()
    (project / ".codex").mkdir()
    hooks_file = project / ".codex" / "hooks.json"
    hooks_file.write_text('{"keep": true}', encoding="utf-8")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, ["doctor", "--fix", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "doctor"
    assert payload["data"]["fix"]["project"]["name"] == "fix-project"
    assert (project / "AGENTS.toml").is_file()
    assert (project / ".codepilot" / "hooks").is_dir()
    assert (project / ".codepilot" / "events").is_dir()
    assert hooks_file.read_text(encoding="utf-8") == '{"keep": true}'
    assert db.get_project("fix-project") is not None
    assert any(item["kind"] == "codex_hooks" and item["status"] == "skipped" for item in payload["data"]["fix"]["actions"])


def test_doctor_fix_with_project_name_registers_current_directory(_isolate_sources, monkeypatch):
    project = _isolate_sources / "worktree"
    project.mkdir()
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, ["doctor", "--fix", "--project", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["fix"]["project"]["name"] == "demo"
    registered = db.get_project("demo")
    assert registered is not None
    assert registered["path"] == str(project.resolve())


def test_doctor_fix_refreshes_incomplete_agents_toml_before_checks(_isolate_sources, monkeypatch):
    project = _isolate_sources / "legacy"
    project.mkdir()
    _write(
        project / "AGENTS.toml",
        """
[project]
name = "legacy"
base_branch = "dev"
""".strip(),
    )
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, ["doctor", "--fix", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    agents_check = next(item for item in payload["data"]["checks"] if item["name"] == "agents_toml")
    assert agents_check["severity"] == "ok"
    config_action = next(item for item in payload["data"]["fix"]["actions"] if item["kind"] == "config")
    assert config_action["status"] == "refreshed"

    import tomllib

    parsed = tomllib.loads((project / "AGENTS.toml").read_text(encoding="utf-8"))
    assert "agents" in parsed
    assert "automation" in parsed


def test_doctor_project_json_emits_doctor_checked_event_to_enabled_sink(_isolate_sources, monkeypatch):
    project = _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "codex"
""".strip(),
    )
    db.init_db()
    db.register_project("demo", str(project), config_file=str(project / "AGENTS.toml"))

    register = CliRunner().invoke(
        main,
        [
            "event",
            "register",
            "-p",
            "demo",
            "--name",
            "doctor-audit",
            "--type",
            "jsonl",
            "--path",
            ".codepilot/events/doctor.jsonl",
            "--event",
            "doctor.checked",
            "--json",
        ],
    )
    assert register.exit_code == 0, register.output

    result = CliRunner().invoke(main, ["doctor", "--project", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["event_delivery"]["delivered"] == 1

    lines = (project / ".codepilot" / "events" / "doctor.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "doctor.checked"
    assert event["source"] == "codepilot.doctor"
    assert event["project"] == "demo"
    assert event["payload"]["ok"] == payload["ok"]
    assert any(item["name"] == "project_config" for item in event["payload"]["checks"])


def test_doctor_project_json_does_not_write_disabled_default_sink(_isolate_sources, monkeypatch):
    project = _project(
        _isolate_sources,
        monkeypatch,
        """
[project]
name = "demo"
base_branch = "dev"

[agents.commands]
codex = "codex"
claude = "claude"

[automation]
planner = "codex"
""".strip(),
    )
    db.init_db()
    db.register_project("demo", str(project), config_file=str(project / "AGENTS.toml"))
    setup = CliRunner().invoke(main, ["setup", str(project), "--json"])
    assert setup.exit_code == 0, setup.output

    result = CliRunner().invoke(main, ["doctor", "--project", "demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["event_delivery"]["delivered"] == 0
    assert payload["data"]["event_delivery"]["results"][0]["status"] == "disabled"
    assert not (project / ".codepilot" / "events" / "events.jsonl").exists()

