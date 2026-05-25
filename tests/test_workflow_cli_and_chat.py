from __future__ import annotations

import contextlib
import json

import pytest
from click.testing import CliRunner

from codepilot.storage import database as db
from codepilot.ai_support import service as ai_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
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
    result = runner.invoke(main, ["go", "--no-execute", "实现一个自动重试机制"])

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

    monkeypatch.setattr("codepilot.core.cli_progress.maybe_cli_renderer", _fake_renderer)
    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", lambda **kwargs: {"ok": True})

    result = CliRunner().invoke(main, ["go", "--no-execute", "实现一个自动重试机制"])

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
    result = runner.invoke(main, ["go", "--agent", "codex", "--no-execute", "实现一个自动重试机制"])

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

    monkeypatch.setattr("codepilot.core.cli_progress.maybe_cli_renderer", _fake_renderer)
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

    result = CliRunner().invoke(main, ["go", "--project", "demo", "config override regression"])

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


def test_batch_add_plain_text_invokes_ai_for_each_title(tmp_path, monkeypatch):
    """纯文本批量（每行一个标题）现在每行都走 AI 生成 + 模板合规校验。

    --no-ai / --allow-empty 已被移除，没有占位通道；mock generate_task_content
    返回模板合规内容确保走通。
    """
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.txt"
    tasks_file.write_text("任务一\n任务二\n", encoding="utf-8")

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    def _fake_generate(title, **kwargs):
        return (
            f"# {title}\n\n## Task Goal\n演示\n\n## In Scope\n- 做一件事\n\n"
            "## Out of Scope\n- 不改无关\n\n## Forbidden (Hard Boundary)\n- 不破坏\n\n"
            "## Files In Scope\n- demo.py\n\n## Planning Evidence\n- 标题猜测\n\n"
            "## Acceptance Criteria\n- [ ] 任务被导入\n\n"
            "## Verification Matrix\n| AC | 命令 | 期望 | 证据 |\n| --- | --- | --- | --- |\n\n"
            "## Reviewer Checkpoints\n- 检查正文非空\n"
        )

    monkeypatch.setattr(add_cmd, "generate_task_content", _fake_generate)

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file)])

    assert result.exit_code == 0, result.output
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 2
    assert all(task["agent"] == "codex" for task in tasks)
    assert all("Task Goal" in (t.get("content") or "") for t in tasks)


def test_batch_add_json_rejects_title_only_items(tmp_path, monkeypatch):
    """JSON 批量必须每条带 content。没有 content 直接拒，不再有占位通道。"""
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
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file)])

    assert result.exit_code != 0
    assert "缺少 content" in result.output
    assert "ai template --format json" in result.output
    assert db.list_tasks(project="demo") == []


def test_batch_add_json_rejects_non_array_payload_without_traceback(tmp_path, monkeypatch):
    """JSON 批量根节点必须是任务数组；单个对象要给可读错误而不是崩溃。"""
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps({"title": "单个对象不是数组"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["add", "-p", "demo", "-f", str(tasks_file)])

    assert result.exit_code != 0
    assert "JSON 批量导入必须是任务数组" in result.output
    assert "Traceback" not in result.output
    assert "AttributeError" not in result.output
    assert db.list_tasks(project="demo") == []


def test_batch_add_plain_text_generation_failure_does_not_create_empty_tasks(tmp_path, monkeypatch):
    """纯文本批量遇到 AI 生成失败时必须 fail-fast，绝不静默写入空任务。

    新语义下 JSON 批量永远不触发 AI（必须自带 content），所以这里覆盖
    plain-text 路径。AI gen 抛异常 → 整批拒绝 → backlog 仍为空。
    """
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    tasks_file = tmp_path / "tasks.txt"
    tasks_file.write_text("生成失败任务\n", encoding="utf-8")

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    def _raise_runtime_error(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(add_cmd, "generate_task_content", _raise_runtime_error)

    runner = CliRunner()
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file)])

    assert result.exit_code != 0
    assert "AI 生成失败" in result.output
    assert db.list_tasks(project="demo") == []


def test_batch_add_json_with_valid_content_imports_directly(tmp_path, monkeypatch):
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
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file)])

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
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file)])

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
    result = runner.invoke(main, ["add", "-p", "demo", "-f", str(tasks_file)])

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
    # The single-add path now enforces task-template compliance, so the
    # mocked AI output must include the same required sections that
    # ai template --format json advertises.
    content = (
        "# 修复任务标题/内容在 Windows 控制台显示乱码\n\n"
        "## Task Goal\n保留中文 UTF-8 在 cmd / PowerShell 下原样可读。\n\n"
        "## In Scope\n- 控制台编码探测\n- 输出 fallback\n\n"
        "## Out of Scope\n- 修改终端字体\n\n"
        "## Forbidden (Hard Boundary)\n- 不要触碰 logger 模块\n\n"
        "## Planning Evidence\n- 用户复现报告 + 现有 console_encoding 模块。\n\n"
        "## Acceptance Criteria\n- 输出标题与正文逐字符等于输入。\n\n"
        "## Verification Matrix\n| AC | 命令 | 预期 | 证据 |\n| --- | --- | --- | --- |\n\n"
        "## Reviewer Checkpoints\n- 检查 echo 链路上是否有 mojibake。\n"
    )

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


def test_add_command_rejects_removed_no_ai_flag(tmp_path, monkeypatch):
    """`--no-ai` 已经被废弃：单条 add 必须由 AI 生成模板合规 content。

    保留这个守卫测试是为了在有人误以为这条偷懒通道还在时，能在 CI 上立刻
    暴露出来。``--allow-empty`` 同样不应再被接受。
    """
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    runner = CliRunner()
    for bad_flag in ("--no-ai", "--allow-empty"):
        result = runner.invoke(main, ["add", "-p", "demo", "-t", "占位任务", bad_flag])
        assert result.exit_code != 0, f"{bad_flag} should be rejected"
        assert "No such option" in result.output, f"{bad_flag} should not be a known option"
    assert db.list_tasks(project="demo") == []


def test_add_command_rejects_ai_content_missing_template_sections(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    monkeypatch.setattr(add_cmd, "check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(add_cmd, "resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))
    # AI returns content that's free-form but lacks the required sections.
    monkeypatch.setattr(
        add_cmd,
        "generate_task_content",
        lambda *args, **kwargs: "# 标题\n\n仅有一段散文，没有 Task Goal / AC / Reviewer 等章节。",
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["add", "-p", "demo", "-t", "缺章节任务", "-a", "codex"],
    )

    assert result.exit_code != 0
    assert "缺少模板必需章节" in result.output
    assert db.list_tasks(project="demo") == []


def test_chat_command_rejects_removed_no_ui_entry(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"])

    assert result.exit_code != 0
    assert "No such option: --no-ui" in result.output


def test_chat_no_ui_does_not_call_old_requirement_workflow(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    called = []
    monkeypatch.setattr(auto_cmd, "run_requirement_workflow", lambda **kwargs: called.append(kwargs))

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="! 做一个自动重试机制\n/exit\n")

    assert result.exit_code != 0
    assert called == []


def test_legacy_chat_repl_helpers_are_not_exposed():
    assert not hasattr(auto_cmd, "_chat_help")
    assert not hasattr(auto_cmd, "run_chat_session")
    assert not hasattr(auto_cmd, "_parse_intent_prefix")


def test_removed_chat_repl_commands_do_not_run_from_main_chat(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    monkeypatch.chdir(project_path)

    called = []
    monkeypatch.setattr(auto_cmd, "render_project_dashboard", lambda *args, **kwargs: called.append((args, kwargs)))

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="/status\n/stats\n/exit\n")

    assert result.exit_code != 0
    assert called == []


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
    result = runner.invoke(main, ["go", "让工具自己优化自己"])

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
