from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from codepilot.storage import database as db
from codepilot.cli import main
from codepilot.commands import inspect as inspect_cmd
from codepilot.core.config import (
    DEFAULT_TEMPLATE,
    GLOBAL_CONFIG_PATH_ENV,
    SECRETS_FILENAME,
    AgentsConfig,
    load_project_config,
    resolve_planner,
    resolve_project_config_reference,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


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

    assert "[classifier]" not in DEFAULT_TEMPLATE
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
    assert cfg.automation.worktree_context_patterns is None
    assert cfg.automation.worktree_context_link_patterns is None
    assert cfg.automation.preflight_dirty_worktree == "stop"
    assert cfg.automation.two_stage_planning is True
    assert cfg.automation.max_review_rounds == 2
    assert cfg.automation.agent_silence_timeout_seconds == 0

    assert cfg.inspect.enabled is False
    assert cfg.inspect.interval_seconds == 1800
    assert cfg.inspect.max_new_tasks_per_round == 3
    assert cfg.inspect.signals == ("git_log", "failed_tasks", "todos")
    assert cfg.inspect.auto_execute is False
    assert cfg.inspect.priority == "P3"
    assert cfg.inspect.planner is None

    assert not hasattr(cfg, "classifier")
    assert cfg.webhook_provider == "auto"
    assert cfg.webhook_secret == ""


def test_config_parses_preflight_dirty_worktree_policy():
    cfg = AgentsConfig.from_dict({"automation": {"preflight_dirty_worktree": "stash"}})

    assert cfg.automation.preflight_dirty_worktree == "stash"


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
planner = "claude"
unknown_agent = "x"

[agents.commands]
codex = "codex-custom"

[automation]
planner = "claude"
task_workspace = "bad-value"
max_tasks = 4
old_flag = true

[automation.scheduled_agents.task_health]
enabled = true
agent = "codex"
interval = "10m"
prompt = "Review local CodePilot task status and summarize risks."
max_cost_usd = 0.1
max_daily_cost_usd = 0.5

[automation.event_agents.failed_task_triage]
enabled = false
trigger = "task.failed"
agent = "codex"
prompt = "Task {{ task_id }} failed with {{ error_message }}. Suggest the smallest repair."
max_cost_usd = 0.1

[classifier]
provider = "deepseek"
timeout = 10

[providers.openai-gpt4o]
enabled = true
model = "gpt-4o"
api_key = "sk-test"
base_url = "https://models.example.invalid/v1"
extra = "drop"

[notifications]
webhook_url = "https://example.invalid/hook"
provider = "feishu"
webhook_secret = "sign-secret"
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

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    parsed = tomllib.loads(synced)
    assert parsed["project"]["name"] == "demo"
    assert parsed["project"]["base_branch"] == "main"
    assert parsed["project"]["default_mode"] == "dual"
    assert parsed["shell"]["preferred"] == "pwsh"
    assert parsed["agents"]["commands"]["codex"] == "codex-custom"
    assert parsed["agents"]["planner"] == "claude"
    assert parsed["automation"]["planner"] == "claude"
    assert parsed["automation"]["task_agent"] == "dual"
    assert parsed["automation"]["executor"] == "builtin"
    assert parsed["automation"]["task_workspace"] == "branch"
    assert parsed["automation"]["worktree_context_patterns"] == [".env*"]
    assert "vendor" not in parsed["automation"]["worktree_context_link_patterns"]
    assert parsed["automation"]["preflight_dirty_worktree"] == "stop"
    assert parsed["automation"]["per_task_branch"] is True
    assert parsed["automation"]["two_stage_planning"] is True
    assert parsed["automation"]["max_review_rounds"] == 2
    assert parsed["automation"]["agent_silence_timeout_seconds"] == 0
    assert parsed["automation"]["scheduled_agents"]["task_health"]["interval"] == "10m"
    assert parsed["automation"]["scheduled_agents"]["task_health"]["prompt"].startswith("Review local")
    assert parsed["automation"]["event_agents"]["failed_task_triage"]["trigger"] == "task.failed"
    assert "{{ error_message }}" in parsed["automation"]["event_agents"]["failed_task_triage"]["prompt"]
    assert "classifier" not in parsed
    assert parsed["inspect"]["signals"] == ["git_log", "failed_tasks", "todos"]
    assert parsed["inspect"]["planner"] == ""
    assert parsed["providers"]["openai-gpt4o"]["model"] == "gpt-4o"
    assert parsed["providers"]["openai-gpt4o"]["api_key"] == "sk-test"
    assert parsed["providers"]["openai-gpt4o"]["base_url"] == "https://models.example.invalid/v1"
    assert parsed["notifications"]["webhook_url"] == "https://example.invalid/hook"
    assert parsed["notifications"]["provider"] == "feishu"
    assert parsed["notifications"]["webhook_secret"] == "sign-secret"


def test_config_sync_removes_legacy_classifier_section_without_adding_provider(tmp_path, monkeypatch):
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

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    parsed = tomllib.loads((project / "AGENTS.toml").read_text(encoding="utf-8"))
    assert "classifier" not in parsed
    assert "deepseek" not in parsed.get("providers", {})


def test_config_sync_preserves_opencode_project_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project / "AGENTS.toml",
        """
[opencode.permission]
mode = "custom"
"*" = "ask"
bash = "allow"
write = "deny"
""".strip(),
    )

    result = CliRunner().invoke(main, ["config", "sync", str(project)])

    assert result.exit_code == 0, result.output

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    parsed = tomllib.loads((project / "AGENTS.toml").read_text(encoding="utf-8"))
    assert parsed["opencode"]["permission"]["mode"] == "custom"
    assert parsed["opencode"]["permission"]["*"] == "ask"
    assert parsed["opencode"]["permission"]["bash"] == "allow"
    assert parsed["opencode"]["permission"]["write"] == "deny"


def test_config_sync_moves_inline_feishu_app_secret_to_sibling_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project / "AGENTS.toml",
        """
[feishu_bot]
enabled = true
app_id = "cli-demo"
app_secret = "feishu-inline-secret"
""".strip(),
    )

    result = CliRunner().invoke(main, ["config", "sync", str(project)])

    assert result.exit_code == 0, result.output

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    agents_data = tomllib.loads((project / "AGENTS.toml").read_text(encoding="utf-8"))
    secrets_data = tomllib.loads((project / SECRETS_FILENAME).read_text(encoding="utf-8"))

    assert agents_data["feishu_bot"]["enabled"] is True
    assert agents_data["feishu_bot"]["app_id"] == "cli-demo"
    assert "app_secret" not in agents_data["feishu_bot"]
    assert ".codepilot.secrets.toml" in (project / "AGENTS.toml").read_text(encoding="utf-8")
    assert secrets_data["feishu_bot"]["app_secret"] == "feishu-inline-secret"
    assert SECRETS_FILENAME in (project / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_config_sync_succeeds_without_existing_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project / "AGENTS.toml",
        """
[project]
name = "demo"
base_branch = "main"

[feishu_bot]
enabled = true
app_id = "cli-demo"
""".strip(),
    )

    result = CliRunner().invoke(main, ["config", "sync", str(project)])

    assert result.exit_code == 0, result.output
    # config sync 会创建 secrets 模板文件，但不应包含实际 secret
    secrets_path = project / SECRETS_FILENAME
    if secrets_path.exists():
        secrets_text = secrets_path.read_text(encoding="utf-8")
        assert "app_secret" not in secrets_text.lower() or '# app_secret' in secrets_text


def test_config_sync_keeps_existing_feishu_secret_file_value(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    project = tmp_path / "demo"
    project.mkdir()
    _write(
        project / "AGENTS.toml",
        """
[feishu_bot]
enabled = true
app_id = "cli-demo"
app_secret = "stale-inline-secret"
""".strip(),
    )
    _write(
        project / SECRETS_FILENAME,
        """
[providers.openai-gpt4o]
api_key = "sk-existing"

[feishu_bot]
app_secret = "feishu-from-secrets"
""".strip(),
    )

    result = CliRunner().invoke(main, ["config", "sync", str(project)])

    assert result.exit_code == 0, result.output

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    secrets_data = tomllib.loads((project / SECRETS_FILENAME).read_text(encoding="utf-8"))
    agents_data = tomllib.loads((project / "AGENTS.toml").read_text(encoding="utf-8"))

    assert "app_secret" not in agents_data["feishu_bot"]
    assert secrets_data["feishu_bot"]["app_secret"] == "feishu-from-secrets"
    assert secrets_data["providers"]["openai-gpt4o"]["api_key"] == "sk-existing"


def test_load_project_config_accepts_registered_project_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv(GLOBAL_CONFIG_PATH_ENV, str(tmp_path / "missing-global-AGENTS.toml"))
    project_path = tmp_path / "project"
    config_root = tmp_path / "config-root"
    project_path.mkdir()
    _write(
        project_path / "AGENTS.toml",
        """
[project]
name = "local"

[automation]
planner = "codex"
""".strip(),
    )
    config_file = config_root / "AGENTS.toml"
    _write(
        config_file,
        """
[project]
name = "registered"

[automation]
planner = "claude"
""".strip(),
    )
    project_info = {
        "name": "demo",
        "path": str(project_path),
        "config_file": str(config_file),
    }

    cfg = load_project_config(project_info)

    assert resolve_project_config_reference(project_info) == str(config_file)
    assert cfg is not None
    assert cfg.project.name == "registered"
    assert cfg.automation.planner == "claude"


def test_inspect_uses_registered_project_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv(GLOBAL_CONFIG_PATH_ENV, str(tmp_path / "missing-global-AGENTS.toml"))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    project_path = tmp_path / "project"
    project_path.mkdir()
    config_file = tmp_path / "config-root" / "AGENTS.toml"
    _write(
        config_file,
        """
[project]
name = "demo"

[inspect]
planner = "claude"
max_new_tasks_per_round = 2
""".strip(),
    )
    db.register_project("demo", str(project_path), config_file=str(config_file))
    captured = {}
    _stub_inspection(monkeypatch, captured)

    result = CliRunner().invoke(
        main,
        ["inspect", "-p", "demo", "--once", "--dry-run", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert captured["demo"] == "claude"


@pytest.mark.parametrize(
    ("cfg_data", "explicit", "expected"),
    [
        (
            {"inspect": {"planner": "claude"}, "agents": {"planner": "codex"}},
            None,
            "claude",
        ),
        (
            {"agents": {"planner": "claude"}},
            None,
            "claude",
        ),
        (
            {},
            None,
            "codex",
        ),
        (
            {"inspect": {"planner": "claude"}, "agents": {"planner": "claude"}},
            "codex",
            "codex",
        ),
        (
            None,
            None,
            "codex",
        ),
    ],
)
def test_resolve_planner_for_inspect_scope(cfg_data, explicit, expected):
    cfg = AgentsConfig.from_dict(cfg_data or {}) if cfg_data is not None else None
    assert resolve_planner(cfg, "inspect", explicit=explicit) == expected
