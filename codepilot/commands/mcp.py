"""CLI commands for serving CodePilot tools over MCP."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.core.output import echo
from codepilot.mcp.server import MCPProjectContext, create_mcp_server
from codepilot.mcp.tool_registry import default_registry
from codepilot.mcp.tools import load_default_tools
from codepilot.storage import database as db


DEFAULT_MCP_HTTP_PORT = 8767
DEFAULT_MCP_HTTP_HOST = "127.0.0.1"


@click.group("mcp")
def mcp_group() -> None:
    """MCP server commands."""


@mcp_group.command("serve")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"], case_sensitive=False),
    default="stdio",
    show_default=True,
    help="MCP transport.",
)
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    default=DEFAULT_MCP_HTTP_PORT,
    show_default=True,
    help="HTTP 监听端口",
)
@click.option("--project", "-p", help="项目名称；不指定时尝试使用当前目录所属项目")
@click.option("--list-tools", is_flag=True, help="列出默认 MCP 工具后退出")
def serve(transport: str, port: int, project: str | None, list_tools: bool) -> None:
    """Start a CodePilot MCP server."""
    load_default_tools()
    context = _resolve_context(project)
    server = create_mcp_server(
        context,
        registry=default_registry,
        include_health=not list_tools,
        bind_sdk=not list_tools,
    )
    if list_tools:
        _print_tool_list(server.list_tools())
        return

    normalized_transport = transport.lower()

    try:
        if normalized_transport == "stdio":
            from codepilot.mcp.stdio_guard import protect_stdio

            with protect_stdio():
                server.run(transport="stdio")
            return

        echo(
            f"[cyan]CodePilot MCP Server[/cyan]  "
            f"http://{DEFAULT_MCP_HTTP_HOST}:{port}/mcp"
        )
        click.echo("  按 Ctrl+C 结束服务。")
        server.run(
            transport="streamable-http",
            host=DEFAULT_MCP_HTTP_HOST,
            port=port,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


def _resolve_context(project: str | None) -> MCPProjectContext:
    db.init_db()
    if project:
        record = db.get_project(project)
        if not record:
            raise click.ClickException(f"项目 '{project}' 未注册")
        return MCPProjectContext(
            project_path=Path(record["path"]),
            project=str(record["name"]),
        )

    record = db.find_project_by_path(Path.cwd())
    if record:
        return MCPProjectContext(
            project_path=Path(record["path"]),
            project=str(record["name"]),
        )
    return MCPProjectContext(project_path=Path.cwd(), project=None)


def _print_tool_list(tools: list[dict[str, object]]) -> None:
    click.echo(f"{len(tools)} MCP tools registered:")
    for tool in tools:
        click.echo(f"- {tool['name']}")
