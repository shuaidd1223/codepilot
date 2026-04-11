from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import CliRunner

from codepilot import db
from codepilot import ai as ai_mod
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

    def fake_run_codex_schema_prompt(prompt, schema, *, project_path="", timeout=240):
        captured["project_path"] = project_path
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
    assert "请做审查" not in captured["cmd"]


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
