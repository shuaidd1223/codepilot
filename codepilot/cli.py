"""CodePilot CLI entrypoint."""

from __future__ import annotations

import importlib
import os

import click

from codepilot import __version__
from codepilot.core.console_encoding import configure_console_encoding
from codepilot.core.runtime import silence_subprocess_windows_if_detached

configure_console_encoding()
silence_subprocess_windows_if_detached()

_LAZY_COMMANDS: dict[str, tuple[str, str]] = {
    "init": ("codepilot.commands.init", "init_"),
    "ai": ("codepilot.commands.ai", "ai"),
    "binary": ("codepilot.commands.binary", "binary"),
    "config": ("codepilot.commands.config_cmd", "config_group"),
    "status": ("codepilot.commands.status", "status"),
    "add": ("codepilot.commands.add", "add"),
    "auto": ("codepilot.commands.auto", "auto"),
    "go": ("codepilot.commands.auto", "go"),
    "chat": ("codepilot.commands.chat", "chat"),
    "plan": ("codepilot.commands.plan", "plan"),
    "requirement-worker": ("codepilot.commands.requirement_worker", "requirement_worker"),
    "run": ("codepilot.commands.run", "run"),
    "ui": ("codepilot.commands.ui", "ui"),
    "daemon": ("codepilot.commands.daemon", "daemon"),
    "inspect": ("codepilot.commands.inspect", "inspect"),
    "explore": ("codepilot.commands.explore", "explore"),
    "wiki": ("codepilot.commands.wiki", "wiki_group"),
    "note": ("codepilot.commands.note", "note_group"),
    "memory": ("codepilot.commands.memory", "memory_group"),
    "trace": ("codepilot.commands.trace", "trace"),
    "webhook": ("codepilot.commands.webhook", "webhook"),
    "feishu": ("codepilot.commands.feishu", "feishu_group"),
    "workflow": ("codepilot.commands.workflow", "workflow_group"),
    "providers": ("codepilot.commands.providers", "providers"),
    "project": ("codepilot.commands.project", "project_group"),
    "cleanup": ("codepilot.commands.cleanup", "cleanup"),
    "doctor": ("codepilot.commands.doctor", "doctor"),
    "event": ("codepilot.commands.event", "event_group"),
    "hook": ("codepilot.commands.hook", "hook_group"),
    "exec": ("codepilot.commands.exec_cmd", "exec_cmd"),
    "self-update": ("codepilot.commands.self_update", "self_update"),
    "build-fix": ("codepilot.commands.build_fix", "build_fix"),
    "shutdown": ("codepilot.commands.shutdown", "shutdown"),
    "skill": ("codepilot.commands.skill", "skill_group"),
    "scheduled": ("codepilot.commands.scheduled", "scheduled_group"),
    "hud": ("codepilot.commands.hud", "hud"),
    "task": ("codepilot.commands.task", "task_group"),
    "mcp": ("codepilot.commands.mcp", "mcp_group"),
}

_REMOVED_COMMAND_HINTS: dict[str, str] = {
    "setup": "codepilot doctor --fix (一键修复所有配置和环境问题)",
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

            # NOTE: 旧版本会自动路由未知命令到 "go" 启动规划管线，
            # 导致输错命令时（如 `codepilot docker`）触发完整的澄清→侦察→任务拆分流程。
            # 现全面重构，不再默认直接走规划，用户应显式使用 `codepilot go "需求"`。
            suggestions = [
                f"使用 `codepilot go \"{first}\"` 提交自然语言需求",
                "使用 `codepilot --help` 查看所有支持的命令",
            ]
            raise click.UsageError(
                f"未知命令: '{first}'。\n" + "\n".join(f"  {s}" for s in suggestions)
            )

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
@click.option("--session", "chat_session", help="无子命令启动 chat 时恢复 CodePilot 隔离 OpenCode 会话")
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
    chat_session: str | None,
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
    ctx.obj["chat_session"] = chat_session
    ctx.obj["planner"] = planner
    ctx.obj["agent"] = agent
    ctx.obj["execute"] = execute
    ctx.obj["executor"] = executor
    ctx.obj["auto_commit"] = auto_commit
    ctx.obj["max_tasks"] = max_tasks
    ctx.obj["max_retries"] = max_retries

    # ── 自安装检测 ───────────────────────────────────────────────
    # 当 PyInstaller 打包的 exe 从非安装目录（如下载目录）运行时，
    # 自动完成：复制到安装目录 + 注册 PATH + 创建默认配置。
    from codepilot.binary_support.paths import executable_name as _executable_name
    from codepilot.binary_support.paths import running_binary_path, default_install_dir

    _running = running_binary_path()
    if _running is not None and not os.environ.get("CODEPILOT_PORTABLE"):
        _install_dir = default_install_dir()
        _installed = _install_dir / _executable_name("codepilot")
        if _running.resolve() != _installed.resolve():
            from codepilot.binary_support.manager import install_binary, ensure_cli_wrappers, ensure_global_config

            _result = install_binary(binary_path=_running, target_dir=_install_dir, register_path=True)
            ensure_global_config()
            ensure_cli_wrappers(_result.target_dir)
            click.echo(
                f"CodePilot 已安装到 {_result.installed_path}。\n"
                "请重新打开终端后运行 codepilot 命令。",
                err=True,
            )
            return
    # ── 自安装检测结束 ───────────────────────────────────────────

    if not json_mode and ctx.invoked_subcommand != "setup":
        from codepilot.storage.database import init_db as _init_db
        _init_db()

    if ctx.invoked_subcommand is None and not ctx.args:
        if chat_session or click.get_text_stream("stdin").isatty():
            chat_cmd = ctx.command.get_command(ctx, "chat")
            if chat_cmd is None:
                raise click.ClickException("未找到 chat 命令")
            ctx.invoke(chat_cmd)
        else:
            click.echo(ctx.get_help())


if __name__ == "__main__":
    main()
