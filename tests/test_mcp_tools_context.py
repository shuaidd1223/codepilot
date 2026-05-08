from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.commands.note import read_notepad
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


def _context_server(project_path: Path):
    import codepilot.mcp.tools.context  # noqa: F401

    return create_mcp_server(
        MCPProjectContext(project_path=project_path, project="demo"),
        registry=default_registry,
        include_health=False,
        bind_sdk=False,
    )


def _tool_by_name(name: str) -> dict[str, Any]:
    import codepilot.mcp.tools.context  # noqa: F401

    return default_registry.get(name).schema


def _error_code(payload: dict[str, Any]) -> str:
    return payload["structuredContent"]["error"]["code"]


def test_context_tools_register_independent_contracts():
    import codepilot.mcp.tools.context  # noqa: F401

    expected = {
        "wiki_query",
        "wiki_add",
        "note_add",
        "explore",
        "inspect_project",
        "hook_trigger",
    }

    names = {tool.name for tool in default_registry.list()}

    assert expected <= names
    for name in expected:
        schema = _tool_by_name(name)
        assert schema["description"]
        assert schema["inputSchema"]["type"] == "object"
        assert schema["inputSchema"]["additionalProperties"] is False
        assert callable(default_registry.get(name).func)


def test_context_tool_input_schemas_capture_required_fields():
    assert _tool_by_name("wiki_query")["inputSchema"]["required"] == ["project", "query"]
    assert _tool_by_name("wiki_add")["inputSchema"]["required"] == ["project", "title", "body"]
    assert _tool_by_name("note_add")["inputSchema"]["required"] == ["project", "content"]
    assert _tool_by_name("explore")["inputSchema"]["required"] == ["project", "query"]
    assert _tool_by_name("inspect_project")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("hook_trigger")["inputSchema"]["required"] == ["project"]

    wiki_props = _tool_by_name("wiki_add")["inputSchema"]["properties"]
    assert wiki_props["project"] == {"type": "string"}
    assert wiki_props["tags"] == {
        "anyOf": [
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ],
        "default": None,
    }


def test_wiki_and_note_context_tools_use_existing_project_memory(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    added = server.call_tool(
        "wiki_add",
        {
            "project": "demo",
            "title": "构建命令",
            "body": "运行 pytest tests/test_mcp_tools_context.py -q",
            "tags": ["test", "mcp"],
        },
    )
    queried = server.call_tool("wiki_query", {"project": "demo", "query": "构建", "limit": 5})
    note = server.call_tool(
        "note_add",
        {"project": "demo", "content": "上下文 MCP 工具复用 note 接口", "section": "priority"},
    )

    assert added["project"] == "demo"
    assert added["page"]["title"] == "构建命令"
    assert queried["project"] == "demo"
    assert queried["results"][0]["title"] == "构建命令"
    assert note["section"] == "priority"
    notepad = read_notepad(db.get_project("demo"))
    assert notepad["sections"]["priority"][0]["content"] == "上下文 MCP 工具复用 note 接口"


def test_explore_context_tool_preserves_readonly_rejection(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    result = server.call_tool("explore", {"project": "demo", "query": "delete a file"})

    assert result["project"]["name"] == "demo"
    assert result["status"] == "rejected"
    assert result["rejected"] is True
    assert result["evidence"] == []
    assert "只读" in result["limitations"][0]


def test_inspect_project_returns_serializable_signal_results(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    from codepilot.commands import inspect as inspect_cmd

    monkeypatch.setattr(inspect_cmd, "collect_git_log", lambda *_: "git-log")
    monkeypatch.setattr(inspect_cmd, "collect_failed_tasks", lambda *_: "failed-tasks")

    result = server.call_tool(
        "inspect_project",
        {"project": "demo", "signals": ["git_log", "failed_tasks"]},
    )

    assert result["project"] == "demo"
    enabled = {item["key"]: item for item in result["signals"] if item["enabled"]}
    assert enabled["git_log"]["content"] == "git-log"
    assert enabled["failed_tasks"]["content"] == "failed-tasks"
    assert all(isinstance(item, dict) for item in result["signals"])


def test_hook_trigger_uses_existing_project_hook_path(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)
    captured: dict[str, Any] = {}

    def _fake_dispatch(project_root, event):
        captured["project_root"] = str(project_root)
        captured["event"] = event
        return [{"name": "audit", "status": "delivered"}]

    monkeypatch.setattr("codepilot.core.event_plugins.dispatch_event_to_sinks", _fake_dispatch)

    result = server.call_tool(
        "hook_trigger",
        {
            "project": "demo",
            "provider": "codex",
            "event_type": "agent.prompt.submitted",
        },
    )

    assert result["project"] == "demo"
    assert result["logged"] is True
    assert result["delivered"] == 1
    assert captured["project_root"] == str(project_path)
    assert captured["event"]["payload"]["provider"] == "codex"
    assert (project_path / ".codepilot" / "hooks" / "logs" / "hook-events.jsonl").is_file()


def test_context_tools_return_structured_errors(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _context_server(project_path)

    missing = server.call_tool("wiki_add", {"project": "demo", "title": "x"})
    wrong_type = server.call_tool("wiki_query", {"project": "demo", "query": "x", "limit": "bad"})
    missing_project = server.call_tool("note_add", {"project": "missing", "content": "x"})
    wiki_business = server.call_tool(
        "wiki_add",
        {"project": "demo", "title": "密钥", "body": "FEISHU_APP_SECRET=abc"},
    )
    note_business = server.call_tool(
        "note_add",
        {"project": "demo", "content": "x", "section": "unknown"},
    )
    hook_business = server.call_tool(
        "hook_trigger",
        {"project": "demo", "provider": "bad-provider"},
    )

    assert _error_code(missing) == "invalid_arguments"
    assert _error_code(wrong_type) == "invalid_arguments"
    assert _error_code(missing_project) == "project_not_found"
    assert _error_code(wiki_business) == "wiki_error"
    assert _error_code(note_business) == "note_error"
    assert _error_code(hook_business) == "hook_error"
