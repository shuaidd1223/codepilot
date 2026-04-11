"""codepilot providers 命令：查看所有可用的 AI Provider."""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from codepilot.ai import (
    CLI_PROVIDERS,
    API_PROVIDERS,
    list_available_providers,
    check_provider_availability,
)


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

    console = Console()

    # CLI Providers
    console.print("\n[bold cyan]CLI Providers[/bold cyan] (命令行 AI)")
    console.print("[dim]需要本地安装对应的 CLI 工具[/dim]\n")

    cli_table = Table(show_header=True, header_style="bold")
    cli_table.add_column("Key", style="cyan", width=15)
    cli_table.add_column("Name", width=25)
    cli_table.add_column("Command", width=15)
    cli_table.add_column("Status", width=20)

    for key, provider in cli_providers:
        is_avail, msg = check_provider_availability(key)
        status = "[green]已安装[/green]" if is_avail else f"[red]{msg}[/red]"

        if available and not is_avail:
            continue

        cli_table.add_row(key, provider.name, provider.cmd, status)

    console.print(cli_table)

    # API Providers
    console.print("\n[bold cyan]API Providers[/bold cyan] (API 接口 AI)")
    console.print("[dim]需要设置对应的 API Key（环境变量或配置文件）[/dim]\n")

    api_table = Table(show_header=True, header_style="bold")
    api_table.add_column("Key", style="cyan", width=18)
    api_table.add_column("Name", width=25)
    api_table.add_column("Type", width=10)
    api_table.add_column("Model", width=25)
    api_table.add_column("Status", width=20)

    for key, provider in api_providers:
        is_avail, msg = check_provider_availability(key)
        status = "[green]可用[/green]" if is_avail else f"[yellow]需配置 Key[/yellow]"

        if available and not is_avail:
            continue

        api_table.add_row(
            key,
            provider.name,
            provider.provider_type,
            provider.model,
            status,
        )

    console.print(api_table)

    # 提示信息
    console.print("\n[dim]环境变量说明:[/dim]")
    console.print("  OPENAI_API_KEY       - OpenAI GPT 系列")
    console.print("  ANTHROPIC_API_KEY    - Claude API")
    console.print("  HUNYUAN_API_KEY      - 腾讯云混元")
    console.print("  ZHIPU_API_KEY        - 智谱 GLM")
    console.print("  ERNIE_API_KEY        - 百度文心")
    console.print("  DASHSCOPE_API_KEY    - 阿里通义")
    console.print("  DEEPSEEK_API_KEY     - DeepSeek")

    console.print("\n[dim]使用示例:[/dim]")
    console.print("  [cyan]codepilot add -p myproj -t \"任务\" -a openai-gpt4o[/cyan]")
    console.print("  [cyan]codepilot add -p myproj -t \"任务\" -a claude-sonnet[/cyan]")
    console.print("  [cyan]codepilot add -p myproj -t \"任务\" -a deepseek[/cyan]")
