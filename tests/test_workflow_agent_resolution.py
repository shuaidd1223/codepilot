from __future__ import annotations



from codepilot.ai_support import service as ai_mod
from codepilot.ai_support.executor_contract import extract_executor_telemetry
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.commands import run_builtin_executor as builtin_exec
from codepilot.core import progress_bus
from codepilot.core.config import load_project_config


def test_normalize_agent_name_preserves_dual():
    assert ai_mod.normalize_agent_name("dual") == "dual"


def test_generate_task_content_uses_codex_for_dual(monkeypatch):
    captured = {}

    monkeypatch.setattr(ai_mod, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(ai_mod, "_collect_project_context", lambda project_path: "")

    def fake_run_cli_provider(provider, prompt, env_overrides=None):
        captured["provider"] = provider.name
        captured["prompt"] = prompt
        return "generated"

    monkeypatch.setattr(ai_mod, "_run_cli_provider", fake_run_cli_provider)

    content = ai_mod.generate_task_content("实现一个自动重试机制", agent="dual")

    assert content == "generated"
    assert captured["provider"] == "OpenAI Codex"
    assert "实现一个自动重试机制" in captured["prompt"]


def test_resolve_task_agent_preserves_dual(monkeypatch):
    monkeypatch.setattr(auto_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(auto_cmd, "_project_config", lambda project_info: None)

    resolved = auto_cmd._resolve_task_agent({"default_mode": "dual", "path": "D:/demo"}, "dual", "builtin")

    assert resolved == "dual"


def test_resolve_task_agent_prefers_automation_task_agent(monkeypatch):
    from codepilot.core.config import AgentsConfig

    cfg = AgentsConfig.from_dict({
        "project": {"default_mode": "dual"},
        "automation": {"task_agent": "claude"},
    })

    monkeypatch.setattr(auto_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))
    monkeypatch.setattr(auto_cmd, "_project_config", lambda project_info: cfg)

    resolved = auto_cmd._resolve_task_agent({"default_mode": "dual", "path": "D:/demo"}, None, "builtin")

    assert resolved == "claude"


def test_resolve_builtin_phase_agent_uses_dual_split():
    assert run_cmd._resolve_builtin_phase_agent("dual", "builder") == ("codex", None)
    assert run_cmd._resolve_builtin_phase_agent("dual", "reviewer") == ("claude", None)


def test_resolve_builtin_phase_agent_uses_configured_dual_split(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = "sonnet"
reviewer = "codex"
""".strip(),
        encoding="utf-8",
    )

    assert run_cmd._resolve_builtin_phase_agent("dual", "builder", project_ref=project_path) == ("claude", "sonnet")
    assert run_cmd._resolve_builtin_phase_agent("dual", "reviewer", project_ref=project_path) == ("codex", None)


def test_agents_config_reads_and_normalizes_dual_phase_agents(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = " codex "
reviewer = " sonnet "
""".strip(),
        encoding="utf-8",
    )

    cfg = load_project_config(project_path)

    assert cfg is not None
    assert cfg.builder == "codex"
    assert cfg.reviewer == "claude-sonnet"


def test_agents_config_treats_blank_dual_phase_agents_as_unset(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[agents]
builder = "   "
reviewer = ""

[agents.commands]
codex = "codex"
claude = "claude"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_project_config(project_path)

    assert cfg is not None
    assert cfg.builder is None
    assert cfg.reviewer is None
    assert cfg.commands["codex"] == "codex"
    assert cfg.commands["claude"] == "claude"


def test_builtin_tooling_fallback_records_executor_telemetry(tmp_path, monkeypatch):
    calls: list[dict] = []
    task_logs: list[dict] = []

    output_dir = tmp_path / "runs"
    output_dir.mkdir()
    task_file = tmp_path / "task.md"
    task_file.write_text("task", encoding="utf-8")
    ctx = builtin_exec._ExecutorContext(
        task={"id": 42, "agent": "dual", "title": "fallback telemetry"},
        project={"name": "demo", "path": str(tmp_path)},
        project_path=tmp_path,
        config_ref=tmp_path,
        output_dir=output_dir,
        task_file=task_file,
        max_rounds=2,
        task_id_for_events=42,
    )

    def fake_run_builtin_phase(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return "codex", 1, "process timed out after 60s"
        return "claude", 0, "fallback ok"

    monkeypatch.setattr(run_cmd, "_run_builtin_phase", fake_run_builtin_phase)
    monkeypatch.setattr(run_cmd, "_write_task_log", lambda *args: task_logs.append({"args": args}))
    monkeypatch.setattr(run_cmd, "check_provider_availability", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(builtin_exec, "_resolve_dual_phase_agents_for_task", lambda *_args, **_kwargs: ("codex", "claude"))

    events: list[dict] = []
    progress_bus.clear_subscribers_for_tests()
    with progress_bus.subscription(events.append):
        agent, exit_code, output, _started = builtin_exec._run_phase_with_tooling_fallback(
            ctx,
            phase="builder",
            round_num=1,
            phase_name="builder",
            label="builder",
            display_phase="builder",
            prompt="do it",
            output_path=output_dir / "builder.md",
            timeout=60,
        )

    assert (agent, exit_code, output) == ("claude", 0, "fallback ok")
    assert calls[1]["agent_override"] == "claude"
    telemetry = extract_executor_telemetry(task_logs[0]["args"][3])
    assert telemetry["fallback_reason"] == "timeout"
    assert telemetry["fallback_path"] == ["codex", "claude"]
    assert telemetry["failed_executor"]["family"] == "codex"
    assert telemetry["fallback_executor"]["family"] == "claude"
    retry_event = next(event for event in events if event["type"] == "phase_retry")
    assert retry_event["extra"]["fallback_reason"] == "timeout"
    assert retry_event["extra"]["fallback_path"] == ["codex", "claude"]
