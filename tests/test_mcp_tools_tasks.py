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

    created = server.call_tool(
        "create_task",
        {
            "project": "demo",
            "title": "实现 MCP 任务工具",
            "content": "任务正文",
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
