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


def test_task_tools_return_structured_errors_for_bad_arguments(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _task_server(project_path)

    missing = server.call_tool("create_task", {"project": "demo"})
    wrong_type = server.call_tool("show_task", {"task_id": "abc"})
    unexpected = server.call_tool("list_tasks", {"project": "demo", "extra": True})

    assert _error_code(missing) == "invalid_arguments"
    assert _error_code(wrong_type) == "invalid_arguments"
    assert _error_code(unexpected) == "invalid_arguments"


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
