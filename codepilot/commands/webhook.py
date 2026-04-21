"""codepilot webhook 命令：启动轻量 HTTP webhook 服务."""

from __future__ import annotations

import click

from codepilot.output import echo
from codepilot.webhook import start_webhook_server


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="监听地址")
@click.option("--port", "-p", type=int, default=8765, help="监听端口，默认 8765")
def webhook(host: str, port: int):
    """
    启动轻量 Webhook 服务，接收外部任务投递.

    POST /tasks  body: {\"project\": \"...\", \"title\": \"...\", \"content\": \"...\"}
    GET  /health
    """
    try:
        server = start_webhook_server(host=host, port=port)
    except OSError as exc:
        raise click.ClickException(f"启动 Webhook 服务失败：{exc}") from exc

    echo(f"[cyan]CodePilot Webhook Server[/cyan]  http://{host}:{port}/")
    click.echo("  POST /tasks   添加任务  body: {project, title, content, priority?, agent?}")
    click.echo("  GET  /health  健康检查")
    click.echo("  按 Ctrl+C 结束服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo()
        echo("[dim]Webhook 服务已停止[/dim]")
    finally:
        server.server_close()
