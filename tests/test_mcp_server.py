from __future__ import annotations

import asyncio
import threading
import warnings
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


def test_tool_dispatch_preserves_async_sync_and_awaitable_without_deprecation(
    tmp_path: Path,
    monkeypatch,
):
    import codepilot.mcp.server as server_module
    from codepilot.mcp.server import MCPProjectContext, create_mcp_server

    registry = ToolRegistry()

    @register_tool(name="demo.async", registry=registry)
    async def async_tool(value: str) -> dict[str, str]:
        return {"value": value}

    @register_tool(name="demo.sync", registry=registry)
    def sync_tool(event_loop_thread: int) -> dict[str, bool]:
        return {"used_worker_thread": threading.get_ident() != event_loop_thread}

    @register_tool(name="demo.awaitable", registry=registry)
    def sync_awaitable_tool(value: str) -> dict[str, str]:
        async def finish() -> dict[str, str]:
            return {"value": value}

        return finish()

    to_thread_calls: list[str] = []
    original_to_thread = server_module.asyncio.to_thread

    async def spy_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(server_module.asyncio, "to_thread", spy_to_thread)

    server = create_mcp_server(
        MCPProjectContext(project_path=tmp_path, project="demo"),
        registry=registry,
        include_health=False,
        bind_sdk=False,
    )

    async def exercise() -> tuple[dict[str, str], dict[str, bool], dict[str, str]]:
        event_loop_thread = threading.get_ident()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            async_result = await server.acall_tool("demo.async", {"value": "ok"})
            sync_result = await server.acall_tool(
                "demo.sync",
                {"event_loop_thread": event_loop_thread},
            )
            awaitable_result = await server.acall_tool(
                "demo.awaitable",
                {"value": "later"},
            )
        return async_result, sync_result, awaitable_result

    assert asyncio.run(exercise()) == (
        {"value": "ok"},
        {"used_worker_thread": True},
        {"value": "later"},
    )
    assert to_thread_calls == ["sync_tool", "sync_awaitable_tool"]
