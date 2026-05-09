from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.storage import database as db

DEFAULT_MCP_TOOL_NAMES = {
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


def _init_project(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    db.init_db()
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def test_mcp_serve_stdio_starts_server_with_project_context(tmp_path, monkeypatch):
    project_path = _init_project(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    from codepilot.commands import mcp as mcp_cmd

    class FakeServer:
        def run(self, **kwargs):
            captured["run_kwargs"] = kwargs

    def fake_create_mcp_server(context, **kwargs):
        captured["context"] = context
        return FakeServer()

    monkeypatch.setattr(mcp_cmd, "create_mcp_server", fake_create_mcp_server)

    result = CliRunner().invoke(
        main,
        ["mcp", "serve", "--transport", "stdio", "--port", "9999", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    assert captured["context"].project == "demo"
    assert captured["context"].project_path == project_path.resolve()
    assert captured["run_kwargs"] == {"transport": "stdio"}


def test_mcp_serve_http_starts_server_on_requested_port(tmp_path, monkeypatch):
    project_path = _init_project(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    from codepilot.commands import mcp as mcp_cmd

    class FakeServer:
        def run(self, **kwargs):
            captured["run_kwargs"] = kwargs

    def fake_create_mcp_server(context, **kwargs):
        captured["context"] = context
        return FakeServer()

    monkeypatch.setattr(mcp_cmd, "create_mcp_server", fake_create_mcp_server)

    result = CliRunner().invoke(
        main,
        ["mcp", "serve", "--transport", "http", "--port", "8765", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    assert captured["context"].project == "demo"
    assert captured["context"].project_path == project_path.resolve()
    assert captured["run_kwargs"] == {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": 8765,
    }


def test_mcp_serve_rejects_invalid_transport(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["mcp", "serve", "--transport", "tcp", "--project", "demo"],
    )

    assert result.exit_code == 2
    assert "Invalid value for '--transport'" in result.output


def test_mcp_serve_uses_default_http_port(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    from codepilot.commands import mcp as mcp_cmd

    class FakeServer:
        def run(self, **kwargs):
            captured["run_kwargs"] = kwargs

    monkeypatch.setattr(mcp_cmd, "create_mcp_server", lambda context, **kwargs: FakeServer())

    result = CliRunner().invoke(
        main,
        ["mcp", "serve", "--transport", "http", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    assert captured["run_kwargs"] == {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": mcp_cmd.DEFAULT_MCP_HTTP_PORT,
    }


def test_mcp_serve_list_tools_outputs_default_registry_tools(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main,
        ["mcp", "serve", "--list-tools", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    assert "21 MCP tools" in result.output
    for name in DEFAULT_MCP_TOOL_NAMES:
        assert f"- {name}" in result.output


def test_mcp_serve_list_tools_includes_phase_4b_tools(tmp_path, monkeypatch):
    _init_project(tmp_path, monkeypatch)
    phase_4b_names = {
        "feishu_notify",
        "feishu_send_to_user",
        "webhook_invoke",
        "doctor",
        "run_once",
        "daemon_status",
        "build_fix",
        "exec",
    }

    result = CliRunner().invoke(
        main,
        ["mcp", "serve", "--list-tools", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    for name in phase_4b_names:
        assert f"- {name}" in result.output


def test_mcp_server_run_applies_http_host_and_port_to_fastmcp(tmp_path):
    from codepilot.mcp.server import MCPProjectContext, create_mcp_server

    class FakeSettings:
        host = "127.0.0.1"
        port = 8000

    class FakeFastMCP:
        def __init__(self, name: str) -> None:
            self.name = name
            self.settings = FakeSettings()
            self.run_kwargs = None

        def tool(self, *, name: str, description: str = ""):
            def decorator(func):
                return func

            return decorator

        def run(self, **kwargs):
            self.run_kwargs = kwargs

    fake_instances: list[FakeFastMCP] = []

    def fake_factory(name: str) -> FakeFastMCP:
        instance = FakeFastMCP(name)
        fake_instances.append(instance)
        return instance

    server = create_mcp_server(
        MCPProjectContext(project_path=tmp_path, project="demo"),
        fastmcp_factory=fake_factory,
    )

    server.run(transport="streamable-http", host="127.0.0.1", port=8765)

    sdk_server = fake_instances[0]
    assert sdk_server.settings.host == "127.0.0.1"
    assert sdk_server.settings.port == 8765
    assert sdk_server.run_kwargs == {"transport": "streamable-http"}
