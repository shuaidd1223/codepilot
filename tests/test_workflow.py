from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import click
from click.testing import CliRunner

from codepilot import binary as binary_mod
from codepilot import db
from codepilot import ai as ai_mod
from codepilot import runtime as runtime_mod
from codepilot.cli import main
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd


def _init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()


def test_update_task_allows_core_fields(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "old title", agent="dual", priority="P2", depends_on=[1])

    updated = db.update_task(
        task["id"],
        title="new title",
        content="new content",
        agent="codex",
        priority="P0",
        depends_on=[2, 3],
    )

    assert updated["title"] == "new title"
    assert updated["content"] == "new content"
    assert updated["agent"] == "codex"
    assert updated["priority"] == "P0"
    assert json.loads(updated["depends_on"]) == [2, 3]


def test_increment_task_retry_eventually_fails(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me", max_retries=2)

    first = db.increment_task_retry(task["id"], "boom")
    second = db.increment_task_retry(task["id"], "boom again")

    assert first["status"] == "backlog"
    assert first["retry_count"] == 1
    assert second["status"] == "failed"
    assert second["retry_count"] == 2


def test_auto_command_creates_linear_subtasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: {
            "summary": "split ok",
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a", "b"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                },
                {
                    "title": "step 2",
                    "priority": "P2",
                    "goal": "do step 2",
                    "acceptance_criteria": ["c", "d"],
                    "builder_notes": ["code 2"],
                    "reviewer_notes": ["review 2"],
                    "files": ["b.py"],
                    "notes": ["note 2"],
                },
            ],
        },
    )

    runner = CliRunner()
    result = runner.invoke(main, ["auto", "-p", "demo", "-t", "big goal", "--plan-only"])

    assert result.exit_code == 0
    tasks = sorted(db.list_tasks(project="demo"), key=lambda item: item["id"])
    assert len(tasks) == 2
    assert tasks[0]["title"] == "step 1"
    assert tasks[0]["depends_on"] is None
    assert tasks[1]["title"] == "step 2"
    assert json.loads(tasks[1]["depends_on"]) == [tasks[0]["id"]]


def test_run_backlog_builtin_stops_after_retry_limit(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=1, output="builder failed", executor="builtin"),
    )

    first = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    assert first["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1

    second = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    assert second["failed"] == 1
    assert current["status"] == "failed"
    assert current["retry_count"] == 2


def test_resolve_project_for_prompt_uses_current_directory(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    project = auto_cmd.resolve_project_for_prompt()

    assert project["name"] == "demo"
    assert project["path"] == str(project_path)


def test_resolve_project_for_prompt_syncs_defaults_from_config(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"
base_branch = "main"
default_mode = "codex"

[automation]
planner = "codex"
executor = "builtin"
auto_execute = true
confirm_before_execute = false
auto_commit = false
max_tasks = 4
max_retries = 2
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path), default_mode="dual")
    monkeypatch.chdir(project_path)

    project = auto_cmd.resolve_project_for_prompt()

    assert project["name"] == "demo"
    assert project["default_mode"] == "codex"


def test_project_config_does_not_leak_from_workspace_when_project_has_no_agents_toml(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))

    auto_cfg = auto_cmd._project_config(db.get_project("demo"))
    run_cfg = run_cmd._project_config(db.get_project("demo"))

    assert auto_cfg is None
    assert run_cfg is None


def test_resolve_project_for_prompt_does_not_overwrite_parent_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "AGENTS.toml").write_text(
        """
[project]
name = "child-demo"
base_branch = "main"
default_mode = "codex"

[automation]
planner = "codex"
executor = "builtin"
auto_execute = true
confirm_before_execute = false
auto_commit = false
max_tasks = 4
max_retries = 2
""".strip(),
        encoding="utf-8",
    )

    db.register_project("parent-demo", str(parent), default_mode="dual")
    monkeypatch.chdir(child)

    project = auto_cmd.resolve_project_for_prompt()
    parent_project = db.get_project("parent-demo")

    assert project["name"] == "child-demo"
    assert project["path"] == str(child)
    assert parent_project["path"] == str(parent)


def test_root_command_accepts_plain_text_requirement(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    captured = {}

    def fake_run_requirement_workflow(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["--no-execute", "实现一个自动重试机制"])

    assert result.exit_code == 0
    assert captured["title"] == "实现一个自动重试机制"
    assert captured["project_info"]["name"] == "demo"
    assert captured["execute"] is False


def test_root_command_passes_selected_task_agent(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    captured = {}

    def fake_run_requirement_workflow(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["--agent", "codex", "--no-execute", "实现一个自动重试机制"])

    assert result.exit_code == 0
    assert captured["task_agent"] == "codex"


def test_batch_add_with_default_agent_does_not_require_click_context(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.txt"
    tasks_file.write_text("任务一\n任务二\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file), "--no-ai"])

    assert result.exit_code == 0
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 2
    assert all(task["agent"] == "codex" for task in tasks)


def test_chat_command_accepts_plain_text_and_exit(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    captured = []

    def fake_run_requirement_workflow(**kwargs):
        captured.append(kwargs["title"])
        return {"ok": True}

    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="做一个自动重试机制\n/exit\n")

    assert result.exit_code == 0
    assert captured == ["做一个自动重试机制"]
    assert "CodePilot Chat" in result.output


def test_chat_command_reports_natural_language_error(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    def fake_run_requirement_workflow(**kwargs):
        raise RuntimeError("当前无法使用 Claude CLI，因为本机没有找到 claude 命令。")

    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="做一个自动重试机制\n/exit\n")

    assert result.exit_code == 0
    assert "当前无法使用 Claude CLI" in result.output
    assert "Traceback" not in result.output


def test_go_command_wraps_runtime_error_as_click_exception(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    def fake_run_requirement_workflow(**kwargs):
        raise RuntimeError("当前无法使用 Claude CLI，因为本机没有找到 claude 命令。")

    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["让工具自己优化自己"])

    assert result.exit_code != 0
    assert "当前无法使用 Claude CLI" in result.output
    assert "Traceback" not in result.output


def test_generate_task_breakdown_uses_codex_planner(monkeypatch):
    captured = {}

    def fake_run_codex_schema_prompt(prompt, schema, *, project_path="", config_ref=None, timeout=240):
        captured["project_path"] = project_path
        captured["config_ref"] = config_ref
        captured["timeout"] = timeout
        return {
            "summary": "ok",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a", "b"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                }
            ],
        }

    monkeypatch.setattr("codepilot.ai._run_codex_schema_prompt", fake_run_codex_schema_prompt)

    breakdown = auto_cmd.generate_task_breakdown(
        title="实现一个自动重试机制",
        project_path="D:/demo",
        planner="codex",
        max_tasks=3,
    )

    assert breakdown["tasks"][0]["title"] == "step 1"
    assert captured["project_path"] == "D:/demo"
    assert captured["config_ref"] is None


def test_run_requirement_workflow_falls_back_to_single_codex_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Codex planning timeout")),
    )

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让工具自己优化自己",
        planner="codex",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert payload["complexity"] == "simple"
    assert payload["should_split"] is False
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["agent"] == "codex"


def test_run_requirement_workflow_uses_registered_config_file_for_provider_resolution(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    config_root = tmp_path / "config-root"
    project_path.mkdir()
    config_root.mkdir()
    config_file = config_root / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
default_mode = "codex"
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path), default_mode="codex", config_file=str(config_file))
    project = db.get_project("demo")
    captured = {}

    def fake_check_provider(agent, project_path=None):
        captured["provider_path"] = project_path
        return True, f"ok:{agent}"

    monkeypatch.setattr(auto_cmd, "check_provider_availability", fake_check_provider)
    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: captured.update({"config_ref": kwargs.get("config_ref")}) or {
            "summary": "ok",
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                }
            ],
        },
    )

    payload = auto_cmd.run_requirement_workflow(
        project_info=project,
        title="让工具自己优化自己",
        planner="codex",
        execute=False,
        executor="builtin",
        auto_commit=False,
    )

    assert payload["tasks"][0]["agent"] == "codex"
    assert captured["provider_path"] == str(config_file)
    assert captured["config_ref"] == str(config_file)


def test_run_requirement_workflow_does_not_fallback_on_non_timeout_codex_error(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path), default_mode="codex")
    project = db.get_project("demo")

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("当前无法使用 Codex，因为本机没有找到 `codex` 命令。")),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        try:
            auto_cmd.run_requirement_workflow(
                project_info=project,
                title="让工具自己优化自己",
                planner="codex",
                execute=False,
                executor="builtin",
                auto_commit=False,
            )
        except Exception as exc:
            assert isinstance(exc, click.ClickException)
            assert "当前无法使用 Codex" in exc.format_message()
        else:
            raise AssertionError("expected ClickException")


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


def test_resolve_builtin_phase_agent_uses_dual_split():
    assert run_cmd._resolve_builtin_phase_agent("dual", "builder") == ("codex", None)
    assert run_cmd._resolve_builtin_phase_agent("dual", "reviewer") == ("claude", None)


def test_run_builtin_phase_codex_review_omits_prompt(monkeypatch, tmp_path):
    captured = {}

    monkeypatch.setattr(run_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))

    class DummyProvider:
        name = "OpenAI Codex"

        @staticmethod
        def find_executable():
            return Path("C:/fake/codex.CMD")

    monkeypatch.setattr(run_cmd, "resolve_cli_provider", lambda provider_key, project_path=None: DummyProvider())

    def fake_run_command(cmd, **kwargs):
        captured["cmd"] = cmd
        return 0, ""

    monkeypatch.setattr(run_cmd, "_run_command", fake_run_command)
    monkeypatch.setattr(run_cmd, "_read_output_file", lambda path: "")

    label, exit_code, output = run_cmd._run_builtin_phase(
        task={"agent": "codex"},
        project_path=tmp_path,
        phase="reviewer",
        prompt="请做审查",
        output_path=tmp_path / "review.txt",
        timeout=30,
    )

    assert label == "codex-review"
    assert exit_code == 0
    assert output == ""
    assert "--uncommitted" in captured["cmd"]
    assert "--ephemeral" in captured["cmd"]
    assert "请做审查" not in captured["cmd"]


def test_extract_review_verdict_uses_codex_review_markers():
    fail_output = """
The change breaks behavior.

Review comment:

- [P1] Keep add returning a sum
""".strip()

    pass_output = "The only change adds a comment and does not affect behavior."

    assert run_cmd._extract_review_verdict(fail_output, "codex-review") == "fail"
    assert run_cmd._extract_review_verdict(pass_output, "codex-review") == "pass"
    assert run_cmd._extract_review_verdict("**VERDICT: FAIL**", "claude-review") == "fail"


def test_run_builtin_executor_fails_when_review_verdict_is_unknown(monkeypatch, tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    task_file = project_path / "task.md"
    task_file.write_text("demo", encoding="utf-8")

    phases = iter(
        [
            ("codex", 0, "builder ok"),
            ("claude-review", 0, "没有输出 verdict"),
        ]
    )

    monkeypatch.setattr(run_cmd, "_builtin_preflight_error", lambda *args, **kwargs: "")
    monkeypatch.setattr(run_cmd, "_run_builtin_phase", lambda **kwargs: next(phases))
    monkeypatch.setattr(run_cmd, "_write_task_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_cmd, "_git_auto_commit", lambda *args, **kwargs: "deadbee")

    result = run_cmd._run_builtin_executor(
        {"id": 7, "title": "demo", "agent": "dual"},
        {"path": str(project_path)},
        task_file,
        auto_commit=False,
    )

    assert result.exit_code == 2
    assert result.summary == "review 结果不明确"


def test_builtin_runtime_dir_is_outside_project(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()

    runtime_dir = run_cmd._builtin_runtime_dir({"name": "demo", "path": str(project_path)})

    assert runtime_dir.exists()
    assert not runtime_dir.is_relative_to(project_path)
    assert ".codepilot" in str(runtime_dir)


def test_run_backlog_builtin_dirty_workspace_requeues_without_retry(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    import subprocess

    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)
    (project_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "blocked by dirty tree", max_retries=2)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert "未提交改动" in (current["error_message"] or "")


def test_generate_task_content_uses_project_configured_codex_cmd(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake_codex = tmp_path / "tools" / "codex.cmd"
    fake_codex.parent.mkdir()
    fake_codex.write_text("@echo off\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        f"""
[project]
name = "demo"

[agents]
codex_cmd = "{fake_codex.as_posix()}"
""".strip(),
        encoding="utf-8",
    )

    captured = {}
    monkeypatch.setattr(ai_mod, "_collect_project_context", lambda project_path: "")

    def fake_run_cli_provider(provider, prompt, env_overrides=None):
        captured["provider_cmd"] = provider.cmd
        captured["prompt"] = prompt
        return "generated"

    monkeypatch.setattr(ai_mod, "_run_cli_provider", fake_run_cli_provider)

    content = ai_mod.generate_task_content(
        "实现一个自动重试机制",
        project_path=str(project_path),
        agent="codex",
    )

    assert content == "generated"
    assert captured["provider_cmd"] == fake_codex.as_posix()
    assert "实现一个自动重试机制" in captured["prompt"]


def test_run_builtin_phase_uses_project_configured_codex_cmd(monkeypatch, tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake_codex = tmp_path / "tools" / "codex.cmd"
    fake_codex.parent.mkdir()
    fake_codex.write_text("@echo off\n", encoding="utf-8")
    (project_path / "AGENTS.toml").write_text(
        f"""
[project]
name = "demo"

[agents]
codex_cmd = "{fake_codex.as_posix()}"
""".strip(),
        encoding="utf-8",
    )

    captured = {}
    monkeypatch.setattr(run_cmd, "check_provider_availability", lambda agent, project_path=None: (True, f"ok:{agent}"))

    def fake_run_command(cmd, **kwargs):
        captured["cmd"] = cmd
        return 0, ""

    monkeypatch.setattr(run_cmd, "_run_command", fake_run_command)
    monkeypatch.setattr(run_cmd, "_read_output_file", lambda path: "")

    label, exit_code, output = run_cmd._run_builtin_phase(
        task={"agent": "codex"},
        project_path=project_path,
        phase="builder",
        prompt="请实现功能",
        output_path=project_path / "builder.txt",
        timeout=30,
    )

    assert label == "codex"
    assert exit_code == 0
    assert output == ""
    assert Path(captured["cmd"][0]) == fake_codex
    assert "--skip-git-repo-check" in captured["cmd"]
    assert "--ephemeral" in captured["cmd"]


def test_run_backlog_builtin_non_git_repo_requeues_without_retry(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "needs git first", max_retries=2)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=True)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert "Git 仓库" in (current["error_message"] or "")


def test_run_backlog_builtin_codex_review_requires_git_even_without_auto_commit(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "needs git for review", agent="codex", max_retries=2)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert "Codex review" in (current["error_message"] or "")


def test_run_backlog_builtin_dual_can_proceed_without_git_when_auto_commit_disabled(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    db.create_task("demo", "dual task", agent="dual", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=0, output="ok", executor="builtin"),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)

    assert stats["done"] == 1


def test_run_backlog_marks_task_cancelled_when_executor_is_stopped(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "cancel me", agent="dual", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: (_ for _ in ()).throw(run_cmd.TaskCancelled("手动停止")),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["cancelled"] == 1
    assert current["status"] == "cancelled"
    assert current["error_message"] == "手动停止"


def test_reap_stalled_tasks_marks_dead_in_progress_task_failed(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stuck task", agent="codex")

    db.update_task(
        task["id"],
        status="in_progress",
        started_at="2026-01-01T00:00:00",
        heartbeat_at="2026-01-01T00:00:00",
        active_pid=None,
        run_phase="builder",
    )

    reaped = runtime_mod.reap_stalled_tasks("demo", stale_after_seconds=1)
    current = db.get_task(task["id"])

    assert len(reaped) == 1
    assert current["status"] == "failed"
    assert "心跳已超过" in (current["error_message"] or "")


def test_stop_command_cancels_in_progress_task_without_live_process(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "stop me", agent="codex")
    db.update_task(task["id"], status="in_progress", active_pid=999999, run_phase="builder")

    monkeypatch.setattr("codepilot.commands.tasks.is_process_alive", lambda pid: False)
    monkeypatch.setattr("codepilot.commands.tasks.stop_process_tree", lambda pid: True)

    runner = CliRunner()
    result = runner.invoke(main, ["stop", str(task["id"])])
    current = db.get_task(task["id"])

    assert result.exit_code == 0
    assert "已停止" in result.output
    assert current["status"] == "cancelled"


def test_logs_command_reads_live_runtime_log(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "show logs", agent="codex")
    log_path = tmp_path / "task.log"
    log_path.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    db.update_task(task["id"], status="in_progress", current_log_path=str(log_path))

    runner = CliRunner()
    result = runner.invoke(main, ["logs", str(task["id"]), "--tail", "2"])

    assert result.exit_code == 0
    assert "line 2" in result.output
    assert "line 3" in result.output


def test_status_verbose_shows_runtime_summary_for_in_progress_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "visible task", agent="codex")
    db.update_task(
        task["id"],
        status="in_progress",
        run_phase="builder",
        heartbeat_at="2999-01-01T00:00:00",
        active_pid=None,
        last_output="running tests",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["status", "-p", "demo", "-v"])

    assert result.exit_code == 0
    assert "builder" in result.output
    assert "running tests" in result.output


def test_extract_error_hint_humanizes_json_payload():
    raw = """{"type":"result","subtype":"success","is_error":true,"result":"You've hit your limit · resets Apr 14, 1pm (Asia/Shanghai)"}"""

    hint = ai_mod._extract_error_hint(raw)

    assert "当前账号额度已用完" in hint
    assert "重置时间 Apr 14, 1pm" in hint
    assert "{" not in hint


def test_root_command_without_args_shows_help_in_non_interactive_mode():
    runner = CliRunner()
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_run_command_renders_plain_text_without_markup():
    runner = CliRunner()
    result = runner.invoke(main, ["run"])

    assert result.exit_code == 0
    assert "错误: 必须指定 --project" in result.output
    assert "[red]" not in result.output


def test_binary_default_install_dir_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    install_dir = binary_mod.default_install_dir()

    assert install_dir == (tmp_path / "LocalAppData" / "Programs" / "CodePilot" / "bin").resolve()


def test_update_project_version_updates_pyproject_and_init(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    init_file = package_dir / "__init__.py"
    pyproject.write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    init_file.write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    previous, current = binary_mod.update_project_version(tmp_path, "0.2.0")

    assert previous == "0.1.0"
    assert current == "0.2.0"
    assert 'version = "0.2.0"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "0.2.0"' in init_file.read_text(encoding="utf-8")


def test_binary_resolve_install_source_prefers_latest_build(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    dist_dir.mkdir(parents=True)
    binary_path = dist_dir / "codepilot.exe"
    binary_path.write_text("exe", encoding="utf-8")

    monkeypatch.setattr(binary_mod, "running_binary_path", lambda: None)
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Windows")

    resolved = binary_mod.resolve_install_source(None, project_root=tmp_path)

    assert resolved == binary_path.resolve()


def test_install_binary_copies_file_and_registers_path(tmp_path, monkeypatch):
    source = tmp_path / "codepilot"
    source.write_text("binary", encoding="utf-8")
    target_dir = tmp_path / "bin"
    captured = {}

    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        binary_mod,
        "register_install_dir",
        lambda directory: captured.update({"directory": Path(directory)}) or (True, "ok"),
    )

    result = binary_mod.install_binary(binary_path=source, target_dir=target_dir, register_path=True)

    assert result.installed_path == (target_dir / "codepilot").resolve()
    assert result.installed_path.exists()
    assert captured["directory"] == target_dir.resolve()


def test_binary_build_command_invokes_pyinstaller(tmp_path, monkeypatch):
    (tmp_path / "codepilot").mkdir()
    (tmp_path / "codepilot" / "__main__.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "codepilot" / "templates").mkdir()
    (tmp_path / "codepilot" / "templates" / "demo.md").write_text("x", encoding="utf-8")
    dist_dir = tmp_path / "dist-out"
    build_dir = tmp_path / "build-out"
    captured = {}

    monkeypatch.setattr(binary_mod, "default_build_dir", lambda root: build_dir)
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(binary_mod.platform, "machine", lambda: "x86_64")

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        binary_path = dist_dir / "codepilot"
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        binary_path.write_text("exe", encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(binary_mod.subprocess, "run", fake_run)

    result = binary_mod.build_binary(project_root=tmp_path, output_dir=dist_dir, clean=True)

    assert result.binary_path == (dist_dir / "codepilot").resolve()
    assert "--onefile" in captured["cmd"]
    assert "--collect-all" in captured["cmd"]


def test_binary_where_command_prints_default_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_INSTALL_DIR", str(tmp_path / "custom-bin"))
    runner = CliRunner()

    result = runner.invoke(main, ["binary", "where"])

    assert result.exit_code == 0
    assert str((tmp_path / "custom-bin").resolve()) in result.output


def test_resolve_release_inputs_uses_dist_binaries_by_default(tmp_path, monkeypatch):
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    linux_dir = tmp_path / "dist" / "binary" / "linux-x86_64"
    win_dir.mkdir(parents=True)
    linux_dir.mkdir(parents=True)
    (win_dir / "codepilot.exe").write_text("exe", encoding="utf-8")
    (linux_dir / "codepilot").write_text("bin", encoding="utf-8")

    resolved = binary_mod.resolve_release_inputs(project_root=tmp_path)

    assert resolved == [
        ("linux-x86_64", (linux_dir / "codepilot").resolve()),
        ("windows-x86_64", (win_dir / "codepilot.exe").resolve()),
    ]


def test_create_release_bundle_generates_manifest_checksums_and_archives(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    output_dir = tmp_path / "release"

    result = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=output_dir,
        version="1.2.3",
    )

    assert result.release_dir == output_dir.resolve()
    assert result.manifest_path.exists()
    assert result.checksum_path.exists()
    assert result.guide_path.exists()
    assert result.summary_path.exists()
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.staged_path.exists()
    assert artifact.archive_path.exists()
    assert artifact.archive_format == "zip"
    install_script = output_dir / "windows-x86_64" / "install-codepilot.cmd"
    assert install_script.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.3"
    assert manifest["artifacts"][0]["platform"] == "windows-x86_64"
    assert manifest["artifacts"][0]["archive_format"] == "zip"
    assert "install_script" in manifest["artifacts"][0]
    checksums = result.checksum_path.read_text(encoding="utf-8")
    assert "windows-x86_64/codepilot.exe" in checksums.replace("\\", "/")
    assert artifact.archive_path.name in checksums
    assert "发布说明" in result.guide_path.read_text(encoding="utf-8")
    assert "发布摘要" in result.summary_path.read_text(encoding="utf-8")


def test_binary_release_command_packages_existing_builds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    win_dir.mkdir(parents=True)
    (win_dir / "codepilot.exe").write_text("exe", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--version", "9.9.9"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-9.9.9"
    assert release_dir.exists()
    assert (release_dir / "release.json").exists()
    assert (release_dir / "SHA256SUMS.txt").exists()
    assert (release_dir / "README.zh-CN.md").exists()
    assert (release_dir / "SUMMARY.zh-CN.md").exists()
    assert (release_dir / "windows-x86_64" / "install-codepilot.cmd").exists()
    assert "guide:" in result.output
    assert "summary:" in result.output


def test_verify_release_bundle_passes_for_valid_release(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "release",
        version="1.0.0",
    )

    verification = binary_mod.verify_release_bundle(release.release_dir)

    assert verification.issues == []
    assert verification.checked_files == 2


def test_binary_verify_command_fails_on_broken_checksum(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "dist" / "release" / "codepilot-1.0.0",
        version="1.0.0",
    )
    release.checksum_path.write_text("broken line\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "verify", "--release-dir", str(release.release_dir)])

    assert result.exit_code != 0
    assert "校验失败" in result.output


def test_verify_release_bundle_fails_when_archive_missing_expected_files(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "release",
        version="1.0.0",
    )
    archive_path = release.artifacts[0].archive_path
    with ZipFile(archive_path, "w") as bundle:
        bundle.writestr("broken/file.txt", "x")

    verification = binary_mod.verify_release_bundle(release.release_dir)

    assert any("压缩包缺少预期文件" in issue or "压缩包校验不匹配" in issue for issue in verification.issues)


def test_binary_release_build_current_merges_new_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    win_dir.mkdir(parents=True)
    existing = win_dir / "codepilot.exe"
    existing.write_text("old", encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "linux-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot"
    built_binary.write_text("new", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="linux-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--build-current", "--version", "2.0.0"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-2.0.0"
    assert (release_dir / "windows-x86_64" / "codepilot.exe").exists()
    assert (release_dir / "linux-x86_64" / "codepilot").exists()


def test_binary_release_build_current_works_without_existing_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--build-current", "--version", "3.0.0"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-3.0.0"
    assert (release_dir / "windows-x86_64" / "codepilot.exe").exists()


def test_create_release_bundle_uses_tar_gz_for_linux(tmp_path):
    binary_path = tmp_path / "codepilot"
    binary_path.write_text("binary", encoding="utf-8")

    result = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("linux-x86_64", binary_path)],
        output_dir=tmp_path / "release-linux",
        version="1.0.0",
    )

    artifact = result.artifacts[0]
    assert artifact.archive_format == "tar.gz"
    assert artifact.archive_path.name.endswith(".tar.gz")
    assert (result.release_dir / "linux-x86_64" / "install-codepilot.sh").exists()


def test_binary_prepare_command_updates_version_and_verifies_release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    (package_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "prepare", "--version", "1.2.0"])

    assert result.exit_code == 0
    assert "版本已更新" in result.output
    assert 'version = "1.2.0"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "1.2.0"' in (package_dir / "__init__.py").read_text(encoding="utf-8")
    assert (tmp_path / "dist" / "release" / "codepilot-1.2.0" / "release.json").exists()
    assert "发布目录校验通过" in result.output


def test_binary_prepare_rolls_back_version_when_build_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    pyproject = tmp_path / "pyproject.toml"
    init_file = package_dir / "__init__.py"
    pyproject.write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    init_file.write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    monkeypatch.setattr("codepilot.commands.binary.build_binary", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("build failed")))

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "prepare", "--version", "2.0.0"])

    assert result.exit_code != 0
    assert 'version = "0.1.0"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "0.1.0"' in init_file.read_text(encoding="utf-8")


def test_release_prepare_alias_invokes_prepare(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    (package_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["release", "prepare", "--version", "1.3.0"])

    assert result.exit_code == 0
    assert (tmp_path / "dist" / "release" / "codepilot-1.3.0" / "release.json").exists()


def test_release_verify_alias_invokes_verify(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "dist" / "release" / "codepilot-1.0.0",
        version="1.0.0",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["release", "verify", "--release-dir", str(release.release_dir)])

    assert result.exit_code == 0
    assert "发布目录校验通过" in result.output
