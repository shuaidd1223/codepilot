"""CodePilot CLI entrypoint."""

from __future__ import annotations

import click

from codepilot import __version__
from codepilot.db import init_db
import codepilot.commands.add as add_cmd
import codepilot.commands.auto as auto_cmd
import codepilot.commands.daemon as daemon_cmd
import codepilot.commands.init as init_cmd
import codepilot.commands.providers as providers_cmd
import codepilot.commands.run as run_cmd
import codepilot.commands.status as status_cmd
import codepilot.commands.tasks as tasks_cmd
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


@click.group(cls=NaturalLanguageGroup, invoke_without_command=True)
@click.version_option(version=__version__)
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
    """CodePilot - pure-text task planning and execution for local engineering workflows."""
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
main.add_command(status_cmd.status)
main.add_command(add_cmd.add)
main.add_command(auto_cmd.auto)
main.add_command(auto_cmd.go)
main.add_command(auto_cmd.chat)
main.add_command(run_cmd.run)
main.add_command(daemon_cmd.daemon)
main.add_command(webhook_cmd.webhook)
main.add_command(providers_cmd.providers)
main.add_command(tasks_cmd.done)
main.add_command(tasks_cmd.edit)
main.add_command(tasks_cmd.rm)
main.add_command(tasks_cmd.find)


if __name__ == "__main__":
    main()
