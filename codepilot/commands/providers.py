"""codepilot providers 命令：查看所有可用的 AI Provider."""

from __future__ import annotations

import click
from rich import box
from rich.table import Table

from codepilot.ai import (
    CLI_PROVIDERS,
    API_PROVIDERS,
    list_available_providers,
    check_provider_availability,
)
from codepilot.output import terminal_console


def _short_status(is_available: bool, message: str | None, *, ok_label: str) -> str:
    if is_available:
        return f"[green]✔ {ok_label}[/green]"
    # Compress the long "原因+提示" into a single short token. Detail goes to a follow-up section.
    short = "需配置 Key" if message and "配置" in message else "不可用"
    return f"[yellow]✘ {short}[/yellow]"


@click.command("providers")
@click.option("--available", "-a", is_flag=True, help="只显示已安装/已配置的")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def providers(ctx: click.Context, available: bool, json_mode: bool):
    """
    查看所有可用的 AI Provider（CLI 和 API）.

    示例:
      codepilot providers           # 查看所有 Provider
      codepilot providers --available  # 只显示已安装的
      codepilot providers --json    # JSON 格式输出
    """
    import json

    # 优先用全局 --json
    if not json_mode and ctx.parent:
        json_mode = ctx.parent.obj.get("json_mode", False)

    cli_providers = list(CLI_PROVIDERS.items())
    api_providers = list(API_PROVIDERS.items())

    if json_mode:
        output = {
            "cli": {},
            "api": {},
        }

        for key, provider in cli_providers:
            available_, msg = check_provider_availability(key)
            output["cli"][key] = {
                "name": provider.name,
                "available": available_,
                "message": msg,
            }

        for key, provider in api_providers:
            available_, msg = check_provider_availability(key)
            output["api"][key] = {
                "name": provider.name,
                "type": provider.provider_type,
                "model": provider.model,
                "base_url": provider.base_url,
                "available": available_,
            }

        click.echo(json.dumps(output, ensure_ascii=False, indent=2))
        return

    console = terminal_console()

    # CLI Providers
    console.print()
    console.print("[bold cyan]CLI Providers[/bold cyan]  [dim]命令行 AI · 需本地安装对应 CLI 工具[/dim]")

    cli_table = Table(show_header=True, header_style="bold bright_black", box=box.SIMPLE_HEAVY, expand=True)
    cli_table.add_column("Key", style="cyan", no_wrap=True, ratio=2)
    cli_table.add_column("名称", no_wrap=True, overflow="ellipsis", ratio=3)
    cli_table.add_column("命令", style="dim", no_wrap=True, ratio=2)
    cli_table.add_column("状态", no_wrap=True, ratio=2)

    cli_unavailable: list[tuple[str, str]] = []
    for key, provider in cli_providers:
        is_avail, msg = check_provider_availability(key)
        if available and not is_avail:
            continue
        cli_table.add_row(key, provider.name, provider.cmd, _short_status(is_avail, msg, ok_label="已安装"))
        if not is_avail and msg:
            cli_unavailable.append((key, msg))

    console.print(cli_table)

    # API Providers
    console.print()
    console.print("[bold cyan]API Providers[/bold cyan]  [dim]API 接口 AI · 需配置 API Key[/dim]")

    api_table = Table(show_header=True, header_style="bold bright_black", box=box.SIMPLE_HEAVY, expand=True)
    api_table.add_column("Key", style="cyan", no_wrap=True, ratio=2)
    api_table.add_column("名称", no_wrap=True, overflow="ellipsis", ratio=3)
    api_table.add_column("类型", no_wrap=True, ratio=1)
    api_table.add_column("模型", no_wrap=True, overflow="ellipsis", ratio=4)
    api_table.add_column("状态", no_wrap=True, ratio=2)

    api_unavailable: list[tuple[str, str]] = []
    for key, provider in api_providers:
        is_avail, msg = check_provider_availability(key)
        if available and not is_avail:
            continue
        api_table.add_row(
            key,
            provider.name,
            provider.provider_type,
            provider.model,
            _short_status(is_avail, msg, ok_label="可用"),
        )
        if not is_avail:
            api_unavailable.append((key, msg or "需配置 API Key"))

    console.print(api_table)

    # 不可用项的详情（只在有不可用时打印）
    if cli_unavailable or api_unavailable:
        console.print()
        console.print("[bold]不可用项详情[/bold]")
        for key, msg in (*cli_unavailable, *api_unavailable):
            console.print(f"  [yellow]·[/yellow] [cyan]{key}[/cyan]  [dim]{msg}[/dim]")

    # 帮助信息
    console.print()
    console.print("[dim]环境变量:[/dim] [cyan]OPENAI_API_KEY[/cyan] · [cyan]ANTHROPIC_API_KEY[/cyan] · "
                  "[cyan]HUNYUAN_API_KEY[/cyan] · [cyan]ZHIPU_API_KEY[/cyan] · "
                  "[cyan]ERNIE_API_KEY[/cyan] · [cyan]DASHSCOPE_API_KEY[/cyan] · [cyan]DEEPSEEK_API_KEY[/cyan]")
    console.print('[dim]示例:[/dim] [cyan]codepilot add -p myproj -t "任务" -a openai-gpt4o[/cyan]')
