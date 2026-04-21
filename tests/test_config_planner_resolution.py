from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codepilot import db
from codepilot.cli import main
from codepilot.commands import inspect as inspect_cmd
from codepilot.config import AgentsConfig


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _register_project(tmp_path, monkeypatch, name: str, agents_toml: str) -> Path:
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()

    project_path = tmp_path / name
    _write(project_path / "AGENTS.toml", agents_toml)
    db.register_project(name, str(project_path))
    return project_path


def _stub_inspection(monkeypatch, captured: dict) -> None:
    def fake_run_inspection(project_info, **kwargs):
        captured[project_info["name"]] = kwargs["planner"]
        return {
            "project": project_info["name"],
            "candidates_total": 0,
            "created": [],
            "skipped": [],
            "auto_execute": kwargs.get("auto_execute", False),
        }

    monkeypatch.setattr(inspect_cmd, "run_inspection", fake_run_inspection)


def test_config_empty_dict_uses_declared_defaults():
    cfg = AgentsConfig.from_dict({})

    assert cfg.project.name == ""
    assert cfg.project.base_branch == "dev"
    assert cfg.project.default_mode == "dual"
    assert cfg.project.worktree_base is None

    assert cfg.shell.preferred == "auto"
    assert cfg.dispatch.dispatch_path == ""
    assert cfg.dispatch.interval_seconds == 600
    assert cfg.dispatch.stale_minutes == 30

    assert cfg.automation.planner == "codex"
    assert cfg.automation.task_agent == "dual"
    assert cfg.automation.executor == "builtin"
    assert cfg.automation.auto_execute is True
    assert cfg.automation.confirm_before_execute is False
    assert cfg.automation.auto_commit is True
    assert cfg.automation.max_tasks == 5
    assert cfg.automation.max_retries == 3
    assert cfg.automation.per_task_branch is True
    assert cfg.automation.task_workspace == "branch"
    assert cfg.automation.two_stage_planning is True
    assert cfg.automation.clarify_vague_requirements is True
    assert cfg.automation.clarify_max_turns == 3
    assert cfg.automation.max_review_rounds == 2
    assert cfg.automation.agent_silence_timeout_seconds == 0

    assert cfg.inspect.enabled is False
    assert cfg.inspect.interval_seconds == 1800
    assert cfg.inspect.max_new_tasks_per_round == 3
    assert cfg.inspect.signals == ("git_log", "failed_tasks", "todos")
    assert cfg.inspect.auto_execute is False
    assert cfg.inspect.priority == "P3"
    assert cfg.inspect.planner is None

    assert cfg.classifier.provider == ""
    assert cfg.classifier.model == ""
    assert cfg.classifier.enabled is True
    assert cfg.classifier.timeout == 30


def test_config_sync_updates_old_config_and_removes_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "demo"
    project.mkdir()
    config_path = project / "AGENTS.toml"
    _write(
        config_path,
        """
[project]
name = "demo"
base_branch = "main"
obsolete = "remove-me"

[shell]
preferred = "pwsh"
unused = true

[agents]
codex_cmd = "codex-custom"
planner = "claude"
unknown_agent = "x"

[automation]
planner = "claude"
task_workspace = "bad-value"
max_tasks = 4
old_flag = true

[providers.openai-gpt4o]
enabled = true
model = "gpt-4o"
api_key = "sk-test"
base_url = "https://models.example.invalid/v1"
extra = "drop"

[notifications]
webhook_url = "https://example.invalid/hook"
extra = "drop"
""".strip(),
    )

    result = CliRunner().invoke(main, ["config", "sync", str(project)])

    assert result.exit_code == 0, result.output
    synced = config_path.read_text(encoding="utf-8")
    assert "obsolete" not in synced
    assert "unknown_agent" not in synced
    assert "old_flag" not in synced
    assert "extra" not in synced
    assert "# 自动规划与执行配置。" in synced
    assert "# 常用 provider 示例。" in synced
    assert "agent_silence_timeout_seconds" in synced
    assert "powershell_path" in synced
    assert "bash_path" in synced
    assert "builder" in synced
    assert "reviewer" in synced
    assert "task_agent" in synced

    import tomllib

    parsed = tomllib.loads(synced)
    assert parsed["project"]["name"] == "demo"
    assert parsed["project"]["base_branch"] == "main"
    assert parsed["project"]["default_mode"] == "dual"
    assert parsed["shell"]["preferred"] == "pwsh"
    assert parsed["agents"]["codex_cmd"] == "codex-custom"
    assert parsed["agents"]["planner"] == "claude"
    assert parsed["automation"]["planner"] == "claude"
    assert parsed["automation"]["task_agent"] == "dual"
    assert parsed["automation"]["executor"] == "builtin"
    assert parsed["automation"]["task_workspace"] == "branch"
    assert parsed["automation"]["per_task_branch"] is True
    assert parsed["automation"]["two_stage_planning"] is True
    assert parsed["automation"]["max_review_rounds"] == 2
    assert parsed["automation"]["agent_silence_timeout_seconds"] == 0
    assert parsed["classifier"]["timeout"] == 30
    assert parsed["inspect"]["signals"] == ["git_log", "failed_tasks", "todos"]
    assert parsed["inspect"]["planner"] == ""
    assert parsed["providers"]["openai-gpt4o"]["model"] == "gpt-4o"
    assert parsed["providers"]["openai-gpt4o"]["api_key"] == "sk-test"
    assert parsed["providers"]["openai-gpt4o"]["base_url"] == "https://models.example.invalid/v1"
    assert parsed["notifications"]["webhook_url"] == "https://example.invalid/hook"


def test_config_sync_adds_provider_section_for_classifier_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project / "AGENTS.toml",
        """
[classifier]
provider = "deepseek"
""".strip(),
    )

    result = CliRunner().invoke(main, ["config", "sync", str(project)])

    assert result.exit_code == 0, result.output

    import tomllib

    parsed = tomllib.loads((project / "AGENTS.toml").read_text(encoding="utf-8"))
    assert parsed["classifier"]["provider"] == "deepseek"
    assert parsed["providers"]["deepseek"]["api_key"] == ""
    assert parsed["providers"]["deepseek"]["model"] == ""
    assert parsed["providers"]["deepseek"]["base_url"] == ""


def test_inspect_prefers_explicit_planner(tmp_path, monkeypatch):
    _register_project(
        tmp_path,
        monkeypatch,
        "demo",
        """
[project]
name = "demo"

[agents]
planner = "codex"

[inspect]
planner = "claude"
""".strip(),
    )
    captured = {}
    _stub_inspection(monkeypatch, captured)

    result = CliRunner().invoke(
        main,
        ["inspect", "-p", "demo", "--planner", "codex", "--once", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert captured["demo"] == "codex"


def test_inspect_uses_inspect_then_agents_planner(tmp_path, monkeypatch):
    _register_project(
        tmp_path,
        monkeypatch,
        "inspect-first",
        """
[project]
name = "inspect-first"

[agents]
planner = "codex"

[inspect]
planner = "claude"
""".strip(),
    )
    _register_project(
        tmp_path,
        monkeypatch,
        "agents-fallback",
        """
[project]
name = "agents-fallback"

[agents]
planner = "claude"
""".strip(),
    )
    captured = {}
    _stub_inspection(monkeypatch, captured)
    runner = CliRunner()

    first = runner.invoke(
        main,
        ["inspect", "-p", "inspect-first", "--once", "--dry-run", "--json"],
    )
    second = runner.invoke(
        main,
        ["inspect", "-p", "agents-fallback", "--once", "--dry-run", "--json"],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert captured["inspect-first"] == "claude"
    assert captured["agents-fallback"] == "claude"


def test_inspect_falls_back_to_codex_without_planner_config(tmp_path, monkeypatch):
    _register_project(
        tmp_path,
        monkeypatch,
        "demo",
        """
[project]
name = "demo"
""".strip(),
    )
    captured = {}
    _stub_inspection(monkeypatch, captured)

    result = CliRunner().invoke(
        main,
        ["inspect", "-p", "demo", "--once", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert captured["demo"] == "codex"
