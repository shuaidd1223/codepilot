"""MCP server adapter for CodePilot tools."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping

from codepilot import __version__
from codepilot.mcp.protocol import CodePilotToolError, normalize_progress_event
from codepilot.mcp.tool_registry import ToolDefinition, ToolRegistry, register_tool


ProgressCallback = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class MCPProjectContext:
    project_path: Path
    project: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_path", Path(self.project_path).resolve())


class CodePilotMCPServer:
    """Thin wrapper that binds CodePilot's registry to an MCP server instance."""

    def __init__(
        self,
        *,
        context: MCPProjectContext,
        registry: ToolRegistry,
        name: str = "CodePilot",
        bind_sdk: bool = True,
        fastmcp_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.context = context
        self.registry = registry
        self.name = name
        self.sdk_server: Any | None = None
        self.sdk_error: Exception | None = None

        if bind_sdk:
            self._bind_sdk(fastmcp_factory)

    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(tool.schema) for tool in self.registry.list()]

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.acall_tool(name, arguments, progress_callback=progress_callback)
            )
        raise RuntimeError("call_tool cannot be used from a running event loop; use acall_tool")

    async def acall_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> Any:
        try:
            return await self._invoke_tool(name, dict(arguments or {}), progress_callback)
        except CodePilotToolError as exc:
            return exc.to_tool_response()

    def run(self, *args: Any, **kwargs: Any) -> Any:
        if self.sdk_server is None:
            raise RuntimeError("MCP SDK is not available; install the mcp package to run the server") from (
                self.sdk_error
            )
        host = kwargs.pop("host", None)
        port = kwargs.pop("port", None)
        if host is not None or port is not None:
            settings = getattr(self.sdk_server, "settings", None)
            if settings is not None:
                if host is not None:
                    settings.host = host
                if port is not None:
                    settings.port = port
        return self.sdk_server.run(*args, **kwargs)

    def _bind_sdk(self, fastmcp_factory: Callable[[str], Any] | None) -> None:
        try:
            self.sdk_server = (
                fastmcp_factory(self.name) if fastmcp_factory is not None else _load_fastmcp(self.name)
            )
        except (ImportError, ModuleNotFoundError) as exc:
            self.sdk_server = None
            self.sdk_error = exc
            return

        for tool in self.registry.list():
            self._register_sdk_tool(tool)

    def _register_sdk_tool(self, tool: ToolDefinition) -> None:
        if self.sdk_server is None:
            return

        signature = inspect.signature(tool.func)

        async def sdk_tool(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)
            return await self.acall_tool(tool.name, bound.arguments)

        sdk_tool.__name__ = _python_identifier(tool.name)
        sdk_tool.__doc__ = tool.schema.get("description") or ""
        sdk_tool.__annotations__ = dict(getattr(tool.func, "__annotations__", {}))
        sdk_tool.__signature__ = signature  # type: ignore[attr-defined]

        self.sdk_server.tool(
            name=tool.name,
            description=tool.schema.get("description") or "",
        )(sdk_tool)

    async def _invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        progress_callback: ProgressCallback | None,
    ) -> Any:
        tool = self.registry.get(name)
        signature = inspect.signature(tool.func)
        bound = signature.bind(**arguments)
        bound.apply_defaults()
        result = tool.func(*bound.args, **bound.kwargs)

        if inspect.isawaitable(result):
            result = await result

        if inspect.isgenerator(result):
            return await self._consume_generator(result, progress_callback)

        if inspect.isasyncgen(result):
            async for event in result:
                await _forward_progress(event, progress_callback)
            return None

        return result

    async def _consume_generator(
        self,
        generator: Any,
        progress_callback: ProgressCallback | None,
    ) -> Any:
        while True:
            try:
                event = next(generator)
            except StopIteration as stop:
                return stop.value
            await _forward_progress(event, progress_callback)


def create_mcp_server(
    context: MCPProjectContext | str | Path,
    *,
    registry: ToolRegistry | None = None,
    include_health: bool = True,
    bind_sdk: bool = True,
    fastmcp_factory: Callable[[str], Any] | None = None,
    name: str = "CodePilot",
) -> CodePilotMCPServer:
    project_context = (
        context if isinstance(context, MCPProjectContext) else MCPProjectContext(Path(context))
    )
    server_registry = build_server_registry(
        context=project_context,
        registry=registry,
        include_health=include_health,
    )
    return CodePilotMCPServer(
        context=project_context,
        registry=server_registry,
        name=name,
        bind_sdk=bind_sdk or fastmcp_factory is not None,
        fastmcp_factory=fastmcp_factory,
    )


def build_server_registry(
    *,
    context: MCPProjectContext,
    registry: ToolRegistry | None = None,
    include_health: bool = True,
) -> ToolRegistry:
    target = ToolRegistry()
    for tool in (registry or ToolRegistry()).list():
        target.register(
            tool.func,
            name=tool.name,
            description=tool.schema.get("description") or "",
        )

    if include_health and not _has_tool(target, "codepilot.health"):
        _register_health_tool(target, context)

    return target


def _register_health_tool(registry: ToolRegistry, context: MCPProjectContext) -> None:
    @register_tool(
        name="codepilot.health",
        description="Return CodePilot MCP server health and version.",
        registry=registry,
    )
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": _package_version(),
            "project": context.project,
            "project_path": str(context.project_path),
        }


def _package_version() -> str:
    try:
        version = metadata.version("codepilot")
    except metadata.PackageNotFoundError:
        version = __version__
    return version or __version__ or "0.0.0"


def _load_fastmcp(name: str) -> Any:
    from mcp.server.fastmcp import FastMCP

    return FastMCP(name)


def _has_tool(registry: ToolRegistry, name: str) -> bool:
    try:
        registry.get(name)
    except KeyError:
        return False
    return True


def _python_identifier(name: str) -> str:
    identifier = "".join(char if char.isalnum() or char == "_" else "_" for char in name)
    if not identifier or identifier[0].isdigit():
        identifier = f"tool_{identifier}"
    return identifier


async def _forward_progress(
    event: Any,
    progress_callback: ProgressCallback | None,
) -> None:
    if progress_callback is None:
        return
    result = progress_callback(normalize_progress_event(event).to_dict())
    if inspect.isawaitable(result):
        await result
