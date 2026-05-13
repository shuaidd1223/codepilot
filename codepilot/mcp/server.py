"""MCP server adapter for CodePilot tools."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping

from codepilot import __version__
from codepilot.mcp.audit import record_mcp_tool_call
from codepilot.mcp.protocol import CodePilotToolError, normalize_progress_event
from codepilot.mcp.tool_registry import ToolDefinition, ToolRegistry, register_tool

# 同步工具函数如果在事件循环线程上执行会阻塞 FastMCP 的
# 心跳 / ping 等协议消息处理，导致客户端超时断开。
# 默认超时 600 秒，防止工具调用无限挂起。
_TOOL_CALL_TIMEOUT: float = 600.0


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
        # 全局 MCP 工具调用超时，防止同步工具阻塞事件循环
        self._tool_timeout: float = _TOOL_CALL_TIMEOUT

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
        call_arguments = dict(arguments or {})
        try:
            result = await asyncio.wait_for(
                self._invoke_tool(name, call_arguments, progress_callback),
                timeout=self._tool_timeout,
            )
        except asyncio.TimeoutError:
            exc = CodePilotToolError(
                f"MCP tool call timed out after {self._tool_timeout}s: {name}",
                code="tool_timeout",
                details={"tool": name, "timeout": self._tool_timeout},
            )
            self._audit_tool_call(
                name,
                call_arguments,
                status="error",
                error={
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            )
            return exc.to_tool_response()
        except CodePilotToolError as exc:
            self._audit_tool_call(
                name,
                call_arguments,
                status="error",
                error={
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            )
            return exc.to_tool_response()
        self._audit_tool_call(name, call_arguments, status="ok", error=None)
        return result

    def run(self, *args: Any, **kwargs: Any) -> Any:
        if self.sdk_server is None:
            raise RuntimeError(_missing_mcp_sdk_message()) from self.sdk_error
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
        try:
            tool = self.registry.get(name)
        except KeyError as exc:
            raise CodePilotToolError(
                f"MCP tool not found: {name}",
                code="tool_not_found",
                details={"tool": name},
            ) from exc
        signature = inspect.signature(tool.func)
        try:
            bound = signature.bind(**arguments)
        except TypeError as exc:
            raise CodePilotToolError(
                str(exc),
                code="invalid_arguments",
                details={"tool": name, "arguments": arguments},
            ) from exc
        bound.apply_defaults()

        func = tool.func
        if asyncio.iscoroutinefunction(func):
            result = await func(*bound.args, **bound.kwargs)
        else:
            # 在独立线程中运行同步工具函数，防止阻塞事件循环
            result = await asyncio.to_thread(func, *bound.args, **bound.kwargs)

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

    def _audit_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        status: str,
        error: Mapping[str, Any] | None,
    ) -> None:
        try:
            record_mcp_tool_call(
                project_path=self.context.project_path,
                tool_name=name,
                arguments=arguments,
                status=status,
                error=error,
            )
        except Exception:
            return


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
            description="返回 CodePilot MCP 服务健康状态和版本。",
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


def _missing_mcp_sdk_message() -> str:
    return (
        "CodePilot MCP 运行依赖未安装：缺少 Python 包 `mcp`。"
        "请在当前 Python 环境执行 `python -m pip install -e .`，"
        "或执行 `python -m pip install \"mcp>=1.27.1\"` 后重试。"
    )


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
