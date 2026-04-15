"""Local Web UI command for CodePilot."""

from __future__ import annotations

import click

from codepilot.output import echo
from codepilot.webui import start_ui_server


@click.command("ui")
@click.option("--host", default="127.0.0.1", show_default=True, help="监听地址")
@click.option("--port", type=int, default=8766, show_default=True, help="监听端口")
@click.option("--open/--no-open", "open_browser", default=True, help="启动后自动打开浏览器")
def ui(host: str, port: int, open_browser: bool):
    """启动本地 Web UI，直接提需求并查看任务、日志和状态。"""
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
