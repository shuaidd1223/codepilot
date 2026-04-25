from __future__ import annotations

import contextlib
import json
import sys
import types
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.agent_support import ai_guide_markdown, command_manifest
from codepilot import binary as binary_mod
from codepilot import binary_paths as binary_paths_mod
from codepilot import db
from codepilot import ai as ai_mod
from codepilot import progress_bus
from codepilot.ai_gateway import GatewayResponse
from codepilot import runtime as runtime_mod
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.config import load_project_config
from tests.workflow_testkit import init_test_db as _init_test_db


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


def test_root_command_wraps_requirement_flow_in_cli_progress_renderer(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    entered: list[tuple[str, bool]] = []

    @contextlib.contextmanager
    def _fake_renderer(*, enabled: bool = True):
        entered.append(("enter", enabled))
        yield
        entered.append(("exit", enabled))

    monkeypatch.setattr("codepilot.cli_progress.maybe_cli_renderer", _fake_renderer)
    monkeypatch.setattr(auto_cmd, "resolve_turn_intent", lambda *args, **kwargs: "requirement")
    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", lambda **kwargs: {"ok": True})

    result = CliRunner().invoke(main, ["--no-execute", "实现一个自动重试机制"])

    assert result.exit_code == 0, result.output
    assert entered == [("enter", True), ("exit", True)]


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


def test_auto_command_wraps_planning_flow_in_cli_progress_renderer(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    entered: list[tuple[str, bool]] = []

    @contextlib.contextmanager
    def _fake_renderer(*, enabled: bool = True):
        entered.append(("enter", enabled))
        yield
        entered.append(("exit", enabled))

    monkeypatch.setattr("codepilot.cli_progress.maybe_cli_renderer", _fake_renderer)
    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", lambda **kwargs: {"ok": True})

    result = CliRunner().invoke(
        main,
        ["auto", "--project", "demo", "--title", "实现一个自动重试机制", "--plan-only"],
    )

    assert result.exit_code == 0, result.output
    assert entered == [("enter", True), ("exit", True)]


def test_plain_text_command_uses_registered_config_file_over_project_agents_toml(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    config_root = tmp_path / "config-root"
    project_path.mkdir()
    config_root.mkdir()
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "local"
default_mode = "codex"

[automation]
planner = "codex"
task_agent = "codex"
executor = "builtin"
auto_execute = true
auto_commit = true
max_tasks = 1
max_retries = 1
two_stage_planning = true
""".strip(),
        encoding="utf-8",
    )
    config_file = config_root / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
default_mode = "dual"

[automation]
planner = "claude"
task_agent = "claude"
executor = "builtin"
auto_execute = false
auto_commit = false
max_tasks = 3
max_retries = 7
two_stage_planning = false
""".strip(),
        encoding="utf-8",
    )
    db.register_project("demo", str(project_path), config_file=str(config_file))
    monkeypatch.chdir(project_path)
    captured = {"provider_calls": []}

    def fake_check_provider(agent, project_path=None):
        captured["provider_calls"].append((agent, project_path))
        return True, f"ok:{agent}"

    def fake_generate_task_breakdown(**kwargs):
        captured["planner_kwargs"] = kwargs
        return {
            "summary": "ok",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "config override step",
                    "priority": "P2",
                    "goal": "exercise project-level config override parsing",
                    "acceptance_criteria": ["a"],
                    "builder_notes": [],
                    "reviewer_notes": [],
                    "files": [],
                    "notes": [],
                }
            ],
        }

    monkeypatch.setattr(auto_cmd, "check_provider_availability", fake_check_provider)
    monkeypatch.setattr(auto_cmd, "generate_task_breakdown", fake_generate_task_breakdown)
    monkeypatch.setattr(auto_cmd, "render_project_dashboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        auto_cmd,
        "run_backlog",
        lambda *args, **kwargs: pytest.fail("auto_execute=false should not run backlog"),
    )

    result = CliRunner().invoke(main, ["--project", "demo", "config override regression"])

    assert result.exit_code == 0, result.output
    assert captured["planner_kwargs"]["planner"] == "claude"
    assert captured["planner_kwargs"]["max_tasks"] == 3
    assert captured["planner_kwargs"]["two_stage"] is False
    assert captured["planner_kwargs"]["config_ref"] == str(config_file)
    assert captured["provider_calls"] == [("claude", str(config_file))]
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 1
    assert tasks[0]["agent"] == "claude"
    assert tasks[0]["max_retries"] == 7


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


def test_batch_add_json_no_ai_rejects_title_only_items(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps([{"title": "只有标题"}], ensure_ascii=False),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file), "--no-ai"])

    assert result.exit_code != 0
    assert "缺少 content" in result.output
    assert "只有标题的空任务" in result.output
    assert db.list_tasks(project="demo") == []


def test_batch_add_json_generation_failure_does_not_create_empty_tasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps([{"title": "生成失败任务"}], ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    def _raise_runtime_error(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(add_cmd, "generate_task_content", _raise_runtime_error)

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file)])

    assert result.exit_code != 0
    assert "AI 生成任务内容失败" in result.output
    assert db.list_tasks(project="demo") == []


def test_batch_add_json_with_valid_content_allows_no_ai(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.json"
    valid_content = """# 示例任务

## Task Goal

让任务详情拥有完整正文。

## In Scope

- 补齐任务模板正文

## Out of Scope

- 不改执行器

## Forbidden (Hard Boundary)

- 不改数据库结构

## Files In Scope

- `codepilot/commands/add.py`

## Planning Evidence

来自批量导入场景复盘。

## Acceptance Criteria

- [ ] 导入后任务正文非空

## Verification Matrix

| AC | Command | Expected | Evidence |
| :--- | :--- | :--- | :--- |
| AC1 | `codepilot task show <id>` | 可见正文 | 任务详情 |

## Reviewer Checkpoints

- 检查任务正文完整
"""
    tasks_file.write_text(
        json.dumps([{"title": "有正文任务", "content": valid_content}], ensure_ascii=False),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file), "--no-ai"])

    assert result.exit_code == 0
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 1
    assert tasks[0]["title"] == "有正文任务"
    assert tasks[0]["content"] == valid_content


def test_batch_add_markdown_imports_multiple_full_tasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.md"
    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    task_one = ai_mod.build_task_markdown_from_plan(
        {
            "title": "Markdown 任务一",
            "agent": "codex",
            "priority": "P1",
            "goal": "验证 markdown 批量导入能识别第一个完整任务。",
            "acceptance_criteria": ["任务一被导入"],
            "builder_notes": ["保留模板结构"],
            "reviewer_notes": ["检查第一条任务正文"],
            "files": ["codepilot/commands/add.py"],
            "notes": ["第一条任务用于验证分隔符不会切碎模板内部的 ---。"],
            "forbidden": ["不要改动无关模块"],
            "not_in_scope": ["不改 Web UI"],
            "evidence": "来自 markdown 批量导入需求。",
        }
    )
    task_two = ai_mod.build_task_markdown_from_plan(
        {
            "title": "Markdown 任务二",
            "agent": "claude-sonnet",
            "priority": "P3",
            "goal": "验证 markdown 批量导入能识别第二个完整任务。",
            "acceptance_criteria": ["任务二被导入"],
            "builder_notes": ["保留模板结构"],
            "reviewer_notes": ["检查第二条任务正文"],
            "files": ["tests/test_workflow_cli_and_chat.py"],
            "notes": ["第二条任务确保真正按任务边界分割。"],
            "forbidden": ["不要改动数据库 schema"],
            "not_in_scope": ["不改执行器"],
            "evidence": "来自 markdown 批量导入需求。",
        }
    )
    tasks_file.write_text(f"{task_one}\n\n---\n\n{task_two}\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file), "--no-ai"])

    assert result.exit_code == 0, result.output
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 2
    assert [task["title"] for task in tasks] == ["Markdown 任务一", "Markdown 任务二"]
    assert tasks[0]["content"].rstrip() == task_one.rstrip()
    assert tasks[1]["content"].rstrip() == task_two.rstrip()
    assert tasks[0]["priority"] == "P1"
    assert tasks[0]["agent"] == "codex"
    assert tasks[1]["priority"] == "P3"
    assert tasks[1]["agent"] == "claude-sonnet"


def test_batch_add_markdown_rejects_tasks_missing_required_sections(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.md"
    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    valid_task = ai_mod.build_task_markdown_from_plan(
        {
            "title": "合规任务",
            "agent": "codex",
            "priority": "P2",
            "goal": "验证首条任务内容完整。",
            "acceptance_criteria": ["首条任务完整"],
            "builder_notes": ["保持模板结构"],
            "reviewer_notes": ["检查模板章节"],
            "files": ["codepilot/templates/task-template.md"],
            "notes": ["这是基线任务。"],
            "forbidden": ["不要改动无关代码"],
            "not_in_scope": ["不改外部文档"],
            "evidence": "来自 markdown 批量导入需求。",
        }
    )
    invalid_task = ai_mod.build_task_markdown_from_plan(
        {
            "title": "缺章节任务",
            "agent": "codex",
            "priority": "P2",
            "goal": "验证缺章节时导入被拒绝。",
            "acceptance_criteria": ["缺少章节会报错"],
            "builder_notes": ["保持模板结构"],
            "reviewer_notes": ["检查失败路径"],
            "files": ["codepilot/commands/add.py"],
            "notes": ["删除必需章节后必须拒绝导入。"],
            "forbidden": ["不要写入半成品任务"],
            "not_in_scope": ["不改 Web UI"],
            "evidence": "来自 markdown 批量导入需求。",
        }
    ).replace("## Verification Matrix", "## Verification Matrix Missing", 1)
    tasks_file.write_text(f"{valid_task}\n\n---\n\n{invalid_task}\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file), "--no-ai"])

    assert result.exit_code != 0
    assert "Markdown 批量导入第 2 项" in result.output
    assert "Verification Matrix" in result.output
    assert db.list_tasks(project="demo") == []


def test_add_command_preserves_utf8_title_and_content_round_trip(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    title = "修复任务标题/内容在 Windows 控制台显示乱码"
    content = "# 任务说明\n\n1. 标题需要原样保留\n2. 内容也要原样保留"

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))
    monkeypatch.setattr(add_cmd, "generate_task_content", lambda *args, **kwargs: content)

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-t", title, "-a", "codex"])

    assert result.exit_code == 0
    created = db.list_tasks(project="demo")
    assert len(created) == 1
    assert created[0]["title"] == title
    assert created[0]["content"] == content


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

    # Pin intent to "task" so the CliRunner's stdin (which on Windows may
    # transcode Chinese through cp1252) can't send us down a different branch.
    monkeypatch.setattr(auto_cmd, "classify_intent", lambda text, **kw: {
        "intent": "task", "source": "forced",
    })
    # Skip multi-turn clarification — this test exercises the straight-to-plan path.
    monkeypatch.setattr(auto_cmd, "clarify_requirement", lambda title, **kw: {
        "status": "ready", "refined_title": title, "qa_history": [],
    })
    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="做一个自动重试机制\n/exit\n")

    assert result.exit_code == 0
    assert len(captured) == 1, f"expected one planner call, got {captured!r}"
    # The encoded title may lose bytes through CliRunner on Windows, but the
    # planner must at least have been invoked with a non-empty title.
    assert captured[0].strip()
    assert "CodePilot Chat" in result.output


def test_chat_command_reports_natural_language_error(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    def fake_run_requirement_workflow(**kwargs):
        raise RuntimeError("当前无法使用 Claude CLI，因为本机没有找到 claude 命令。")

    monkeypatch.setattr(auto_cmd, "classify_intent", lambda text, **kw: {
        "intent": "task", "source": "forced",
    })
    monkeypatch.setattr(auto_cmd, "clarify_requirement", lambda title, **kw: {
        "status": "ready", "refined_title": title, "qa_history": [],
    })
    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", fake_run_requirement_workflow)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="做一个自动重试机制\n/exit\n")

    assert result.exit_code == 0
    assert "当前无法使用 Claude CLI" in result.output
    assert "Traceback" not in result.output


def test_chat_help_mentions_stats_command():
    assert "/stats" in auto_cmd._chat_help()


def test_chat_status_renders_dashboard(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    called = {}

    monkeypatch.setattr(auto_cmd, "render_project_dashboard", lambda *args, **kwargs: called.update({"args": args, "kwargs": kwargs}))

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="/status\n/exit\n")

    assert result.exit_code == 0
    assert called["args"][0] == "demo"
    assert "demo" in called["kwargs"]["title"]


def test_chat_stats_outputs_status_summary(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    db.create_task("demo", "task backlog")
    running = db.create_task("demo", "task running")
    done = db.create_task("demo", "task done")
    failed = db.create_task("demo", "task failed")
    cancelled = db.create_task("demo", "task cancelled")

    db.update_task(running["id"], status="in_progress")
    db.update_task(done["id"], status="done")
    db.update_task(failed["id"], status="failed")
    db.update_task(cancelled["id"], status="cancelled")

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="/stats\n/exit\n")

    assert result.exit_code == 0
    assert "状态统计  demo" in result.output
    assert "进行中:1" in result.output
    assert "待办:1" in result.output
    assert "失败:1" in result.output
    assert "已取消:1" in result.output
    assert "完成:1" in result.output
    assert "总计:5" in result.output


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


def test_root_command_without_args_shows_help_in_non_interactive_mode():
    runner = CliRunner()
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_root_help_includes_ui_command():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "ui" in result.output


def test_find_json_accepts_options_after_keyword(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    db.create_task("demo", "needle task", content="contains needle")

    runner = CliRunner()
    result = runner.invoke(main, ["task", "find", "needle", "-p", "demo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "find"
    assert payload["data"]["count"] == 1
    assert payload["data"]["tasks"][0]["title"] == "needle task"


def test_run_command_renders_plain_text_without_markup():
    runner = CliRunner()
    result = runner.invoke(main, ["run"])

    assert result.exit_code == 0
    assert "错误: 必须指定 --project" in result.output
    assert "[red]" not in result.output
