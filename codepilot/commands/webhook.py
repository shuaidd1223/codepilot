"""codepilot webhook 命令：Webhook 服务（占位，后续接入 FastAPI/HTTP Server）."""

from __future__ import annotations

import json

import click

from codepilot import db
from codepilot.output import echo


@click.command()
@click.option("--port", "-p", type=int, default=8765, help="监听端口，默认 8765")
def webhook(port: int):
    """
    启动轻量 Webhook 服务，接收外部任务投递.

    POST /tasks  body: {\"project\": \"...\", \"title\": \"...\", \"content\": \"...\"}
    GET  /health

    当前状态：打印服务信息，实际 HTTP 服务待实现.
    """
    echo(f"[cyan]CodePilot Webhook Server[/cyan]  端口: {port}")
    click.echo()
    click.echo("Webhook 路由:")
    click.echo("  POST /tasks   添加任务  body: {project, title, content}")
    click.echo("  GET  /health  健康检查")
    click.echo()
    echo("[yellow]HTTP 服务待实现（可选：FastAPI / http.server）[/yellow]")
    echo()
    echo("[dim]当前可用方式：codepilot add 命令直接添加任务[/dim]")
    click.echo()

    # TODO: 实际 HTTP 服务（使用标准库 http.server 或 FastAPI）
    # 示例路由：
    # POST /tasks -> db.create_task(...)
    # GET  /health -> {"status": "ok"}
