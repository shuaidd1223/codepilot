from __future__ import annotations

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.mcp.server import MCPProjectContext, create_mcp_server
from codepilot.mcp.tool_registry import default_registry
from codepilot.mcp.tools import load_default_tools


PHASE_4B_TOOL_NAMES = {
    "build_fix",
    "daemon_status",
    "doctor",
    "exec",
    "feishu_notify",
    "feishu_send_to_user",
    "run_once",
    "webhook_invoke",
}

EXPECTED_DEFAULT_MCP_TOOLS = {
    "archive_task",
    "build_fix",
    "create_task",
    "daemon_status",
    "doctor",
    "edit_task",
    "exec",
    "explore",
    "feishu_notify",
    "feishu_send_to_user",
    "generate_breakdown",
    "hook_trigger",
    "inspect_project",
    "list_tasks",
    "note_add",
    "run_once",
    "show_task",
    "stop_task",
    "webhook_invoke",
    "wiki_add",
    "wiki_query",
}

FORBIDDEN_DRIFT_TOOL_NAMES = {
    "chat",
    "chat_send",
    "chat_session_create",
    "scheduled_agent_create",
    "scheduled_agent_run",
    "scheduled_agent_update",
    "scheduled_agents",
}


def _assert_no_chat_or_scheduled_agent_tools(names: set[str]) -> None:
    assert names.isdisjoint(FORBIDDEN_DRIFT_TOOL_NAMES)
    assert all(not name.startswith("chat") for name in names)
    assert all("scheduled" not in name for name in names)


def test_default_list_tools_exposes_exact_21_tool_contracts_without_phase_4b_drift(tmp_path):
    load_default_tools()
    server = create_mcp_server(
        MCPProjectContext(project_path=tmp_path, project=None),
        registry=default_registry,
        include_health=False,
        bind_sdk=False,
    )

    tools = server.list_tools()
    names = {tool["name"] for tool in tools}

    assert len(tools) == 21
    assert names == EXPECTED_DEFAULT_MCP_TOOLS
    assert PHASE_4B_TOOL_NAMES <= names
    _assert_no_chat_or_scheduled_agent_tools(names)
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


def test_mcp_list_tools_cli_prints_the_same_exact_21_tool_names():
    result = CliRunner().invoke(main, ["mcp", "serve", "--list-tools"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    listed_names = {line.removeprefix("- ") for line in lines[1:]}

    assert lines[0] == "21 MCP tools registered:"
    assert listed_names == EXPECTED_DEFAULT_MCP_TOOLS
    _assert_no_chat_or_scheduled_agent_tools(listed_names)
