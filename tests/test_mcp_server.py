from __future__ import annotations

from pathlib import Path

from codepilot.mcp.protocol import CodePilotToolError, ProgressEvent
from codepilot.mcp.tool_registry import ToolRegistry, register_tool


def test_create_server_registers_health_without_starting_transport(tmp_path: Path):
    from codepilot.mcp.server import MCPProjectContext, create_mcp_server

    class FakeFastMCP:
        instances: list["FakeFastMCP"] = []

        def __init__(self, name: str) -> None:
            self.name = name
            self.registered: list[str] = []
            self.run_called = False
            FakeFastMCP.instances.append(self)

        def tool(self, *, name: str, description: str = ""):
            def decorator(func):
                self.registered.append(name)
                return func

            return decorator

        def run(self, *args, **kwargs) -> None:
            self.run_called = True

    server = create_mcp_server(
        MCPProjectContext(project_path=tmp_path, project="demo"),
        fastmcp_factory=FakeFastMCP,
    )

    assert server.context.project == "demo"
    assert "codepilot.health" in [tool["name"] for tool in server.list_tools()]
    assert FakeFastMCP.instances[0].registered == ["codepilot.health"]
    assert FakeFastMCP.instances[0].run_called is False


def test_health_tool_round_trips_through_server_call_path(tmp_path: Path):
    from codepilot.mcp.server import MCPProjectContext, create_mcp_server

    server = create_mcp_server(
        MCPProjectContext(project_path=tmp_path, project="demo"),
        bind_sdk=False,
    )

    payload = server.call_tool("codepilot.health", {})

    assert payload["ok"] is True
    assert payload["version"]
    assert payload["project"] == "demo"
    assert payload["project_path"] == str(tmp_path)


def test_tool_calls_return_errors_and_forward_progress_events(tmp_path: Path):
    from codepilot.mcp.server import MCPProjectContext, create_mcp_server

    registry = ToolRegistry()

    @register_tool(name="demo.echo", registry=registry)
    def echo(value: str) -> dict[str, str]:
        return {"value": value}

    @register_tool(name="demo.fail", registry=registry)
    def fail() -> dict[str, str]:
        raise CodePilotToolError(
            "project not found",
            code="project_missing",
            details={"project": "missing"},
        )

    @register_tool(name="demo.progress", registry=registry)
    def progress() -> dict[str, str]:
        yield ProgressEvent(message="starting", progress=0, total=2)
        yield {"message": "finishing", "progress": 2, "total": 2}
        return {"status": "done"}

    server = create_mcp_server(
        MCPProjectContext(project_path=tmp_path, project="demo"),
        registry=registry,
        include_health=False,
        bind_sdk=False,
    )

    events: list[dict] = []

    assert server.call_tool("demo.echo", {"value": "pong"}) == {"value": "pong"}
    assert server.call_tool("demo.progress", {}, progress_callback=events.append) == {
        "status": "done"
    }
    assert events == [
        {"type": "progress", "message": "starting", "progress": 0, "total": 2},
        {"type": "progress", "message": "finishing", "progress": 2, "total": 2},
    ]

    error = server.call_tool("demo.fail", {})

    assert error == {
        "isError": True,
        "content": [{"type": "text", "text": "project not found"}],
        "structuredContent": {
            "error": {
                "code": "project_missing",
                "message": "project not found",
                "details": {"project": "missing"},
            }
        },
    }
