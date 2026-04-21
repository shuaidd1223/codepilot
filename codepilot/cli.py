"""CodePilot CLI entrypoint."""

from __future__ import annotations

import click

from codepilot import __version__
from codepilot.console_encoding import configure_console_encoding
from codepilot.runtime import silence_subprocess_windows_if_detached

configure_console_encoding()
silence_subprocess_windows_if_detached()

from codepilot.db import init_db
import codepilot.commands.add as add_cmd
import codepilot.commands.ai as ai_cmd
import codepilot.commands.auto as auto_cmd
import codepilot.commands.binary as binary_cmd
import codepilot.commands.config_cmd as config_cmd
import codepilot.commands.daemon as daemon_cmd
import codepilot.commands.init as init_cmd
import codepilot.commands.providers as providers_cmd
import codepilot.commands.project as project_cmd
import codepilot.commands.release as release_cmd
import codepilot.commands.run as run_cmd
import codepilot.commands.status as status_cmd
import codepilot.commands.tasks as tasks_cmd
import codepilot.commands.ui as ui_cmd
import codepilot.commands.webui_service as webui_service_cmd
import codepilot.commands.cleanup as cleanup_cmd
import codepilot.commands.doctor as doctor_cmd
import codepilot.commands.webhook as webhook_cmd


class NaturalLanguageGroup(click.Group):
    """Treat unknown top-level input as a plain-text requirement."""

    def resolve_command(self, ctx, args):
        if args:
            cmd = self.get_command(ctx, args[0])
            if cmd is not None:
                return args[0], cmd, args[1:]

            go_cmd = self.get_command(ctx, "go")
            if go_cmd is not None:
                return "go", go_cmd, args

        return super().resolve_command(ctx, args)


def _cn_help_option():
    def callback(ctx, param, value):
        if value and not ctx.resilient_parsing:
            click.echo(ctx.get_help(), color=ctx.color)
            ctx.exit()
    return click.option(
        "--help",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=callback,
        help="显示此帮助信息并退出",
    )


@click.group(cls=NaturalLanguageGroup, invoke_without_command=True, add_help_option=False)
@click.version_option(version=__version__, message="%(prog)s %(version)s", help="显示版本号并退出")
@_cn_help_option()
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="以 JSON 格式输出（全局选项）")
@click.option("--project", "direct_project", help="纯文本模式下使用的项目名，不指定则自动识别")
@click.option("--planner", default=None, help="纯文本模式下的规划器，默认读取配置")
@click.option("--agent", default=None, help="纯文本模式下创建任务时使用的智能体，如 codex / claude / dual")
@click.option("--execute/--no-execute", default=None, help="纯文本模式下是否立即执行，默认读取配置")
@click.option(
    "--executor",
    type=click.Choice(["auto", "dispatch", "builtin"], case_sensitive=False),
    default=None,
    help="纯文本模式下的执行器，默认读取配置",
)
@click.option("--auto-commit/--no-auto-commit", default=None, help="纯文本模式下是否自动提交，默认读取配置")
@click.option("--max-tasks", type=int, default=0, help="纯文本模式下最大拆分任务数，0 表示读取配置")
@click.option("--max-retries", type=int, default=0, help="纯文本模式下最大重试次数，0 表示读取配置")
@click.pass_context
def main(
    ctx: click.Context,
    json_mode: bool,
    direct_project: str | None,
    planner: str | None,
    agent: str | None,
    execute: bool | None,
    executor: str | None,
    auto_commit: bool | None,
    max_tasks: int,
    max_retries: int,
):
    """CodePilot —— 面向本地工程工作流的纯文本任务规划与执行工具."""
    ctx.ensure_object(dict)
    ctx.obj["json_mode"] = json_mode
    ctx.obj["direct_project"] = direct_project
    ctx.obj["planner"] = planner
    ctx.obj["agent"] = agent
    ctx.obj["execute"] = execute
    ctx.obj["executor"] = executor
    ctx.obj["auto_commit"] = auto_commit
    ctx.obj["max_tasks"] = max_tasks
    ctx.obj["max_retries"] = max_retries

    if not json_mode:
        init_db()

    if ctx.invoked_subcommand is None and not ctx.args:
        if click.get_text_stream("stdin").isatty():
            ctx.invoke(auto_cmd.chat)
        else:
            click.echo(ctx.get_help())


main.add_command(init_cmd.init_)
main.add_command(ai_cmd.ai)
main.add_command(binary_cmd.binary)
main.add_command(config_cmd.config_group)
main.add_command(release_cmd.release)
main.add_command(status_cmd.status)
main.add_command(add_cmd.add)
main.add_command(auto_cmd.auto)
main.add_command(auto_cmd.go)
main.add_command(auto_cmd.chat)
main.add_command(run_cmd.run)
main.add_command(ui_cmd.ui)
main.add_command(webui_service_cmd.webui)
main.add_command(daemon_cmd.daemon)
from codepilot.commands import inspect as inspect_cmd  # noqa: E402
main.add_command(inspect_cmd.inspect)
main.add_command(webhook_cmd.webhook)
main.add_command(providers_cmd.providers)
main.add_command(project_cmd.project_group)
main.add_command(cleanup_cmd.cleanup)
main.add_command(doctor_cmd.doctor)
main.add_command(tasks_cmd.done)
main.add_command(tasks_cmd.retry)
main.add_command(tasks_cmd.cancel)
main.add_command(tasks_cmd.resume)
main.add_command(tasks_cmd.edit)
main.add_command(tasks_cmd.rm)
main.add_command(tasks_cmd.find)
main.add_command(tasks_cmd.stop)
main.add_command(tasks_cmd.sweep)
main.add_command(tasks_cmd.logs)


if __name__ == "__main__":
    main()
