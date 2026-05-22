from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.mcp.server import MCPProjectContext, create_mcp_server
from codepilot.mcp.tool_registry import default_registry
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _init_demo_project(tmp_path: Path, monkeypatch) -> Path:
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def _task_server(project_path: Path):
    import codepilot.mcp.tools.tasks  # noqa: F401

    return create_mcp_server(
        MCPProjectContext(project_path=project_path, project="demo"),
        registry=default_registry,
        include_health=False,
        bind_sdk=False,
    )


def _tool_by_name(name: str) -> dict[str, Any]:
    import codepilot.mcp.tools.tasks  # noqa: F401

    return default_registry.get(name).schema


def _error_code(payload: dict[str, Any]) -> str:
    return payload["structuredContent"]["error"]["code"]


def test_task_tools_register_independent_contracts():
    import codepilot.mcp.tools.tasks  # noqa: F401

    expected = {
        "create_task",
        "list_tasks",
        "show_task",
        "edit_task",
        "stop_task",
        "archive_task",
        "generate_breakdown",
        "validate_task_template",
    }

    names = {tool.name for tool in default_registry.list()}

    assert expected <= names
    for name in expected:
        schema = _tool_by_name(name)
        assert schema["description"]
        assert schema["inputSchema"]["type"] == "object"
        assert schema["inputSchema"]["additionalProperties"] is False
        assert callable(default_registry.get(name).func)


def test_task_tool_input_schemas_capture_required_fields():
    assert _tool_by_name("create_task")["inputSchema"]["required"] == ["project", "title"]
    assert _tool_by_name("show_task")["inputSchema"]["required"] == ["task_id"]
    assert _tool_by_name("edit_task")["inputSchema"]["required"] == ["task_id"]
    assert _tool_by_name("stop_task")["inputSchema"]["required"] == ["task_id"]
    assert _tool_by_name("archive_task")["inputSchema"]["required"] == ["task_id"]
    assert _tool_by_name("generate_breakdown")["inputSchema"]["required"] == [
        "project",
        "requirement",
    ]

    create_props = _tool_by_name("create_task")["inputSchema"]["properties"]
    assert create_props["project"] == {"type": "string"}
    assert create_props["title"] == {"type": "string"}
    assert create_props["depends_on"] == {
        "anyOf": [
            {"type": "array", "items": {"type": "integer"}},
            {"type": "null"},
        ],
        "default": None,
    }


def test_task_tools_round_trip_create_list_show_and_archive(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)

    complete_content = """# 实现 MCP 任务工具

## Task Goal

测试任务创建流程。

## In Scope

- 任务创建
- 任务列表
- 任务详情

## Out of Scope

无

## Forbidden (Hard Boundary)

无

## Planning Evidence

无

## Acceptance Criteria

1. 可以创建任务
2. 可以列出任务
3. 可以查看任务详情

## Verification Matrix

无

## Reviewer Checkpoints

无
"""
    created = server.call_tool(
        "create_task",
        {
            "project": "demo",
            "title": "实现 MCP 任务工具",
            "content": complete_content,
            "priority": "P1",
            "depends_on": [1, 2],
        },
    )

    assert created["task"]["id"]
    assert created["task"]["project"] == "demo"
    assert created["task"]["priority"] == "P1"
    assert created["task"]["depends_on_ids"] == [1, 2]

    listed = server.call_tool("list_tasks", {"project": "demo", "status": "backlog"})
    assert listed["count"] == 1
    assert listed["tasks"][0]["id"] == created["task"]["id"]

    shown = server.call_tool("show_task", {"task_id": created["task"]["id"]})
    assert shown["task"]["title"] == "实现 MCP 任务工具"
    assert shown["task"]["depends_on_ids"] == [1, 2]

    db.update_task(created["task"]["id"], status="done")
    archived = server.call_tool("archive_task", {"task_id": created["task"]["id"]})
    assert archived["task"]["status"] == "archived"


def test_edit_task_updates_allowed_fields(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)
    task = db.create_task(
        project="demo",
        title="旧标题",
        content="正文",
        priority="P3",
        agent="dual",
        depends_on=[1],
    )

    edited = server.call_tool(
        "edit_task",
        {
            "task_id": task["id"],
            "title": "新标题",
            "priority": "p1",
            "status": "failed",
            "agent": "codex",
            "depends_on": [2, 3],
        },
    )

    assert edited["updated_fields"] == [
        "active_pid",
        "agent",
        "current_log_path",
        "depends_on",
        "heartbeat_at",
        "last_output",
        "priority",
        "run_phase",
        "status",
        "stop_reason",
        "stop_requested",
        "title",
    ]
    assert edited["task"]["title"] == "新标题"
    assert edited["task"]["priority"] == "P1"
    assert edited["task"]["status"] == "failed"
    assert edited["task"]["agent"] == "codex"
    assert edited["task"]["depends_on_ids"] == [2, 3]


def test_stop_task_cancels_running_task_via_runtime_api(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)
    task = db.create_task(project="demo", title="运行中", project_path=str(project_path))
    db.update_task(
        task["id"],
        status="in_progress",
        active_pid=12345,
        run_phase="builder",
        worktree_path=str(project_path),
    )
    stopped_pids: list[int] = []

    monkeypatch.setattr(
        "codepilot.core.runtime.stop_process_tree",
        lambda pid: stopped_pids.append(pid) or True,
    )
    monkeypatch.setattr("codepilot.core.runtime.is_process_alive", lambda pid: False)
    monkeypatch.setattr(
        "codepilot.core.runtime.stop_worktree_leftovers",
        lambda *args, **kwargs: None,
    )

    stopped = server.call_tool("stop_task", {"task_id": task["id"], "message": "用户停止"})

    assert stopped_pids == [12345]
    assert stopped["stop_requested"] is False
    assert stopped["task"]["status"] == "cancelled"
    assert stopped["task"]["active_pid"] is None
    assert stopped["task"]["stop_requested"] == 0
    assert stopped["task"]["error_message"] == "用户停止"


def test_task_mcp_mutations_reject_current_runner_task(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)
    task = db.create_task(project="demo", title="runner owned", project_path=str(project_path))
    db.update_task(task["id"], status="in_progress", run_phase="builder", active_pid=12345)
    monkeypatch.setenv("CODEPILOT_RUNNER_TASK_ID", str(task["id"]))
    monkeypatch.setenv("CODEPILOT_RUNNER_PHASE", "builder")

    edit_result = server.call_tool("edit_task", {"task_id": task["id"], "status": "done"})
    archive_result = server.call_tool("archive_task", {"task_id": task["id"]})
    stop_result = server.call_tool("stop_task", {"task_id": task["id"], "message": "stop"})
    current = db.get_task(task["id"])

    assert _error_code(edit_result) == "runner_task_status_owned"
    assert _error_code(archive_result) == "runner_task_status_owned"
    assert _error_code(stop_result) == "runner_task_status_owned"
    assert current["status"] == "in_progress"
    assert current["active_pid"] == 12345


def test_generate_breakdown_uses_registered_project_and_existing_tasks(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)
    existing = db.create_task(project="demo", title="已有任务", project_path=str(project_path))
    captured: dict[str, Any] = {}

    def _fake_generate_task_breakdown(
        requirement: str,
        *,
        project_path: str,
        planner: str,
        max_tasks: int,
        existing_tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        captured.update(
            {
                "requirement": requirement,
                "project_path": project_path,
                "planner": planner,
                "max_tasks": max_tasks,
                "existing_task_ids": [task["id"] for task in existing_tasks],
            }
        )
        return {
            "summary": "ok",
            "should_split": True,
            "tasks": [{"title": "子任务", "goal": "接通正常路径"}],
        }

    monkeypatch.setattr(
        "codepilot.ai_support.service.generate_task_breakdown",
        _fake_generate_task_breakdown,
    )

    result = server.call_tool(
        "generate_breakdown",
        {
            "project": "demo",
            "requirement": "接通任务系统工具",
            "planner": "codex",
            "priority": "p0",
            "max_tasks": 3,
        },
    )

    assert result["project"] == "demo"
    assert result["priority"] == "P0"
    assert result["breakdown"]["tasks"][0]["title"] == "子任务"
    assert captured == {
        "requirement": "接通任务系统工具",
        "project_path": str(project_path),
        "planner": "codex",
        "max_tasks": 3,
        "existing_task_ids": [existing["id"]],
    }


def test_task_tools_return_structured_errors_for_bad_arguments(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)

    missing = server.call_tool("create_task", {"project": "demo"})
    wrong_type = server.call_tool("show_task", {"task_id": "abc"})
    unexpected = server.call_tool("list_tasks", {"project": "demo", "extra": True})
    empty_breakdown_requirement = server.call_tool(
        "generate_breakdown",
        {"project": "demo", "requirement": ""},
    )

    assert _error_code(missing) == "invalid_arguments"
    assert _error_code(wrong_type) == "invalid_arguments"
    assert _error_code(unexpected) == "invalid_arguments"
    assert _error_code(empty_breakdown_requirement) == "invalid_arguments"


def test_task_tools_return_structured_errors_for_task_state(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)
    task = db.create_task(project="demo", title="待办", project_path=str(project_path))

    not_found = server.call_tool("show_task", {"task_id": 99999})
    invalid_status = server.call_tool(
        "edit_task",
        {"task_id": task["id"], "status": "not-a-status"},
    )
    invalid_archive = server.call_tool("archive_task", {"task_id": task["id"]})

    assert _error_code(not_found) == "task_not_found"
    assert _error_code(invalid_status) == "invalid_task_status"
    assert _error_code(invalid_archive) == "invalid_task_status"


def test_create_task_rejects_missing_template_sections(tmp_path, monkeypatch):
    """create_task 必须拒绝缺少 task-template 必填章节的 content。"""
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)

    # 缺少 Task Goal、In Scope、Acceptance Criteria 等章节
    result = server.call_tool(
        "create_task",
        {
            "project": "demo",
            "title": "功能需求",
            "content": "## 标题\n\n简单描述",
        },
    )

    # 验证返回错误 code
    assert _error_code(result) == "missing_template_sections"

    # 验证错误信息明确列出缺少的章节
    error_msg = result["structuredContent"]["error"]["message"]
    assert "Task Goal" in error_msg
    assert "In Scope" in error_msg
    assert "Acceptance Criteria" in error_msg


def test_create_task_succeeds_with_complete_template_sections(tmp_path, monkeypatch):
    """content 包含全部必填章节时 create_task 可以成功创建。"""
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)

    complete_content = """# 功能实现

## Task Goal

实现任务模板校验功能。

## In Scope

- MCP create_task 校验
- 错误信息返回

## Out of Scope

- 不改模板本身

## Forbidden (Hard Boundary)

不涉及的领域

## Planning Evidence

无

## Acceptance Criteria

1. 缺少章节时返回错误
2. 错误信息列出缺失章节

## Verification Matrix

无

## Reviewer Checkpoints

无
"""
    result = server.call_tool(
        "create_task",
        {
            "project": "demo",
            "title": "完整任务",
            "content": complete_content,
        },
    )

    assert "task" in result
    assert result["task"]["id"]
    assert result["task"]["title"] == "完整任务"


def test_validate_task_template_reports_missing_sections(tmp_path, monkeypatch):
    """validate_task_template 必须报告 content 中缺少的章节。"""
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)

    incomplete = "## 标题\n\n简单描述"
    result = server.call_tool("validate_task_template", {"content": incomplete})

    assert result["is_valid"] is False
    assert isinstance(result["missing"], list)
    assert "Task Goal" in result["missing"]
    assert "In Scope" in result["missing"]
    assert "Acceptance Criteria" in result["missing"]


def test_validate_task_template_passes_complete_content(tmp_path, monkeypatch):
    """包含全部必填章节的 content 应通过校验。"""
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)

    complete = """# 功能实现

## Task Goal

实现模板校验。

## In Scope

- 校验逻辑

## Out of Scope

- 无

## Forbidden (Hard Boundary)

- 无

## Planning Evidence

- 无

## Acceptance Criteria

1. 校验通过

## Verification Matrix

- 无

## Reviewer Checkpoints

- 无
"""
    result = server.call_tool("validate_task_template", {"content": complete})

    assert result["is_valid"] is True
    assert result["missing"] == []


def test_validate_task_template_allows_empty_content(tmp_path, monkeypatch):
    """空 content（无任务内容）应视为有效（允许创建后补充）。"""
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)

    result = server.call_tool("validate_task_template", {"content": ""})

    # 空 content 允许通过（create_task 已有拒绝逻辑兜底）
    assert result["is_valid"] is True
    assert result["missing"] == []
