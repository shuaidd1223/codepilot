"""CodePilot CLI entrypoint."""

from __future__ import annotations

import importlib

import click

from codepilot import __version__
from codepilot.core.console_encoding import configure_console_encoding
from codepilot.core.runtime import silence_subprocess_windows_if_detached

configure_console_encoding()
silence_subprocess_windows_if_detached()

from codepilot.storage.database import init_db

_LAZY_COMMANDS: dict[str, tuple[str, str]] = {
    "init": ("codepilot.commands.init", "init_"),
    "ai": ("codepilot.commands.ai", "ai"),
    "binary": ("codepilot.commands.binary", "binary"),
    "config": ("codepilot.commands.config_cmd", "config_group"),
    "status": ("codepilot.commands.status", "status"),
    "add": ("codepilot.commands.add", "add"),
    "auto": ("codepilot.commands.auto", "auto"),
    "go": ("codepilot.commands.auto", "go"),
    "chat": ("codepilot.commands.auto", "chat"),
    "clarify": ("codepilot.commands.clarify", "clarify"),
    "plan": ("codepilot.commands.plan", "plan"),
    "requirement-worker": ("codepilot.commands.requirement_worker", "requirement_worker"),
    "run": ("codepilot.commands.run", "run"),
    "ui": ("codepilot.commands.ui", "ui"),
    "daemon": ("codepilot.commands.daemon", "daemon"),
    "inspect": ("codepilot.commands.inspect", "inspect"),
    "explore": ("codepilot.commands.explore", "explore"),
    "wiki": ("codepilot.commands.wiki", "wiki_group"),
    "note": ("codepilot.commands.note", "note_group"),
    "trace": ("codepilot.commands.trace", "trace"),
    "webhook": ("codepilot.commands.webhook", "webhook"),
    "feishu": ("codepilot.commands.feishu", "feishu_group"),
    "workflow": ("codepilot.commands.workflow", "workflow_group"),
    "providers": ("codepilot.commands.providers", "providers"),
    "project": ("codepilot.commands.project", "project_group"),
    "cleanup": ("codepilot.commands.cleanup", "cleanup"),
    "doctor": ("codepilot.commands.doctor", "doctor"),
    "hud": ("codepilot.commands.hud", "hud"),
    "task": ("codepilot.commands.task", "task_group"),
}

_REMOVED_COMMAND_HINTS: dict[str, str] = {
    "release": "codepilot binary <subcommand>",
    "show": "codepilot task show <task_id>",
    "done": "codepilot task done <task_id>",
    "retry": "codepilot task retry <task_id>",
    "archive": "codepilot task archive <task_id...>",
    "cancel": "codepilot task cancel <task_id...>",
    "resume": "codepilot task resume <task_id...>",
    "edit": "codepilot task edit <task_id> [options]",
    "rm": "codepilot task rm <task_id...>",
    "find": "codepilot task find <keyword>",
    "stop": "codepilot task stop <task_id>",
    "sweep": "codepilot task sweep <task_id>",
    "logs": "codepilot task logs <task_id>",
    "webui": "codepilot ui <start|status|logs|stop|restart>",
}


def _load_lazy_command(name: str):
    spec = _LAZY_COMMANDS.get(name)
    if not spec:
        return None
    module_name, attr_name = spec
    module = importlib.import_module(module_name)
    return getattr(module, attr_name, None)


class NaturalLanguageGroup(click.Group):
    """Treat unknown top-level input as a plain-text requirement."""

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command

        loaded = _load_lazy_command(cmd_name)
        if loaded is not None:
            # Register once after lazy import so subsequent lookups are cheap.
            self.add_command(loaded, name=cmd_name)
            return super().get_command(ctx, cmd_name)
        return None

    def list_commands(self, ctx):
        static = set(super().list_commands(ctx))
        static.update(_LAZY_COMMANDS.keys())
        return sorted(static)

    def resolve_command(self, ctx, args):
        if args:
            first = args[0]
            cmd = self.get_command(ctx, first)
            if cmd is not None:
                return first, cmd, args[1:]

            replacement = _REMOVED_COMMAND_HINTS.get(first)
            if replacement:
                raise click.UsageError(f"命令 `{first}` 已移除，请使用 `{replacement}`。")

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
            chat_cmd = ctx.command.get_command(ctx, "chat")
            if chat_cmd is None:
                raise click.ClickException("未找到 chat 命令")
            ctx.invoke(chat_cmd)
        else:
            click.echo(ctx.get_help())


if __name__ == "__main__":
    main()

