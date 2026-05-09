from __future__ import annotations


PHASE_4A_TASK_TOOLS = {
    "create_task",
    "list_tasks",
    "show_task",
    "edit_task",
    "stop_task",
    "archive_task",
    "generate_breakdown",
}

PHASE_4A_CONTEXT_TOOLS = {
    "wiki_query",
    "wiki_add",
    "note_add",
    "explore",
    "inspect_project",
    "hook_trigger",
}


def _structured_payload(result: dict) -> dict:
    return result["structuredContent"]


def _error_code(result: dict) -> str:
    return result["structuredContent"]["error"]["code"]


def test_mcp_stdio_smoke_covers_health_task_context_and_errors(mcp_stdio_smoke_server):
    tools = {tool["name"] for tool in mcp_stdio_smoke_server.list_tools()}

    assert "codepilot.health" in tools
    assert PHASE_4A_TASK_TOOLS <= tools
    assert PHASE_4A_CONTEXT_TOOLS <= tools

    health = _structured_payload(mcp_stdio_smoke_server.call_tool("codepilot.health"))
    assert health["ok"] is True
    assert health["project"] == "demo"
    assert health["project_path"] == str(mcp_stdio_smoke_server.project_path.resolve())

    created = _structured_payload(
        mcp_stdio_smoke_server.call_tool(
            "create_task",
            {
                "project": "demo",
                "title": "MCP stdio smoke task",
                "content": "created through tools/call",
                "priority": "P1",
            },
        )
    )
    assert created["task"]["id"]
    assert created["task"]["project"] == "demo"
    assert created["task"]["priority"] == "P1"

    note = _structured_payload(
        mcp_stdio_smoke_server.call_tool(
            "note_add",
            {
                "project": "demo",
                "content": "MCP stdio smoke context note",
                "section": "priority",
            },
        )
    )
    assert note["project"] == "demo"
    assert note["section"] == "priority"
    assert note["entry"]["content"] == "MCP stdio smoke context note"

    missing = mcp_stdio_smoke_server.call_tool("show_task", {"task_id": 999999})
    assert missing["isError"] is True
    assert _error_code(missing) == "task_not_found"

    assert mcp_stdio_smoke_server.ping() == {}
