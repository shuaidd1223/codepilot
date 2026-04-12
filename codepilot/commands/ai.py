"""AI-facing command manifest and usage guides."""

from __future__ import annotations

import click

from codepilot.agent_support import ai_guide_markdown, ai_prompt_text, manifest_json, runtime_command_name


@click.group("ai")
def ai():
    """输出供其他 AI/Agent 直接读取的命令清单和调用说明。"""


@ai.command("manifest")
@click.option("--indent", type=int, default=2, help="JSON 缩进空格数")
@click.option("--version", default=None, help="覆盖输出中的版本号")
@click.option("--command-name", default=None, help="覆盖输出中的命令名，例如 codepilot 或 mypilot")
@click.option("--binary-name", default="codepilot", help="覆盖示例中的二进制文件名，例如 codepilot 或 mypilot")
def ai_manifest(indent: int, version: str | None, command_name: str | None, binary_name: str):
    """输出机器可读的命令清单 JSON。"""
    click.echo(
        manifest_json(
            indent=indent,
            version=version,
            command_name=command_name or runtime_command_name(),
            binary_name=binary_name,
        )
    )


@ai.command("guide")
@click.option("--command-name", default=None, help="覆盖输出中的命令名，例如 codepilot 或 mypilot")
def ai_guide(command_name: str | None):
    """输出面向其他 AI 的 Markdown 使用手册。"""
    click.echo(ai_guide_markdown(command_name=command_name or runtime_command_name()))


@ai.command("prompt")
@click.option("--command-name", default=None, help="覆盖输出中的命令名，例如 codepilot 或 mypilot")
def ai_prompt(command_name: str | None):
    """输出给其他 AI 的短提示词。"""
    click.echo(ai_prompt_text(command_name=command_name or runtime_command_name()))
