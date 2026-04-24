"""AI-facing command manifest and usage guides."""

from __future__ import annotations

import click

from codepilot.agent_support import (
    ai_guide_markdown,
    ai_prompt_text,
    manifest_json,
    runtime_command_name,
    task_template_guide_markdown,
    task_template_schema_json,
    _task_template_markdown,
)


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


@ai.command("template")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["md", "json", "guide"], case_sensitive=False),
    default="md",
    help="输出格式：md（原始模板，默认） / json（机器可读 schema） / guide（中文填充指南）",
)
@click.option("--indent", type=int, default=2, help="JSON 缩进空格数，仅对 --format json 生效")
@click.option("--command-name", default=None, help="覆盖输出中的命令名，例如 codepilot 或 mypilot")
def ai_template(fmt: str, indent: int, command_name: str | None):
    """输出任务模板，外部 AI / Web 批量添加任务时必须遵循本模板格式。

    \b
    适用场景：
      * 外部 AI 自己规划了任务，想通过 `add -f tasks.json` 直接投递，
        而不走 CodePilot 自带的规划器。
      * 外部脚本调用 Web UI 批量添加任务。
      * 人工只走自然语言入口（`codepilot "需求文本"`），不直接 add。

    \b
    三种输出：
      * --format md      原始 task-template.md（含占位符 {title}, {goal} 等）
      * --format json    字段 schema + 批量导入格式（机器可读）
      * --format guide   中文填充指南（Markdown，含示例）
    """
    command = command_name or runtime_command_name()
    fmt_normalized = fmt.lower()
    if fmt_normalized == "json":
        click.echo(task_template_schema_json(indent=indent, command_name=command))
    elif fmt_normalized == "guide":
        click.echo(task_template_guide_markdown(command_name=command))
    else:
        click.echo(_task_template_markdown())
