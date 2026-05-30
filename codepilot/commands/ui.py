"""Local Web UI command for CodePilot."""

from __future__ import annotations

import click

from codepilot.commands.feishu import ensure_service_running_if_enabled
from codepilot.commands.webui_service import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    WEBUI_HOST_ENV,
    WEBUI_PORT_ENV,
    _resolve_start_host_port,
    logs_cmd,
    restart_cmd,
    start_cmd,
    status_cmd,
    stop_cmd,
)
from codepilot.core.output import echo
from codepilot.storage import database as db
from codepilot.webapp.server import start_ui_server


def _project_info(project: str) -> dict | None:
    if not project:
        return None
    db.init_db()
    return db.get_project(project)


def _serve_foreground(*, host: str, port: int, open_browser: bool, project: str = "") -> None:
    """Run the Web UI HTTP server in foreground."""
    project_ref = _project_info(project)
    if project and not project_ref:
        echo(f"[yellow]项目 {project} 未注册，跳过飞书服务自动启动。[/yellow]")
        feishu = {"enabled": False, "running": False, "started": False}
    else:
        feishu = ensure_service_running_if_enabled(project_ref) if project_ref else ensure_service_running_if_enabled()
    if feishu.get("error"):
        echo(f"[yellow]飞书服务自动启动失败：{feishu['error']}[/yellow]")
    elif feishu.get("started"):
        echo(f"[dim]飞书服务已自动启动，PID={feishu.get('pid')}[/dim]")

    try:
        server = start_ui_server(host=host, port=port, open_browser=open_browser)
    except OSError as exc:
        raise click.ClickException(f"启动 Web UI 失败：{exc}") from exc

    echo(f"[cyan]CodePilot Web UI[/cyan]  http://{host}:{port}/")
    click.echo("  可查看项目列表、直接提需求 / 建任务，并执行重试 / 停止 / 插队。")
    click.echo("  按 Ctrl+C 结束服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo()
        echo("[dim]Web UI 已停止[/dim]")
    finally:
        server.server_close()


@click.group("ui", invoke_without_command=True)
@click.option("--host", default=None, help=f"监听地址，默认读取 {WEBUI_HOST_ENV} 或 {DEFAULT_HOST}")
@click.option("--port", type=int, default=None, help=f"监听端口，默认读取 {WEBUI_PORT_ENV} 或 {DEFAULT_PORT}")
@click.option("--open/--no-open", "open_browser", default=True, help="启动后自动打开浏览器")
@click.option("--project", "-p", default="", help="使用指定项目配置自动启动关联服务")
@click.pass_context
def ui(ctx: click.Context, host: str | None, port: int | None, open_browser: bool, project: str):
    """Web UI 统一入口（前台启动 + 服务管理）。"""
    if ctx.invoked_subcommand is not None:
        return
    resolved_host, resolved_port = _resolve_start_host_port(host, port)
    _serve_foreground(host=resolved_host, port=resolved_port, open_browser=open_browser, project=project)


ui.add_command(start_cmd)
ui.add_command(stop_cmd)
ui.add_command(restart_cmd)
ui.add_command(status_cmd)
ui.add_command(logs_cmd)
