"""CodePilot CLI 入口."""

from __future__ import annotations

import json
import click

from codepilot import __version__
from codepilot.db import init_db
import codepilot.commands.init as init_cmd
import codepilot.commands.status as status_cmd
import codepilot.commands.add as add_cmd
import codepilot.commands.run as run_cmd
import codepilot.commands.daemon as daemon_cmd
import codepilot.commands.webhook as webhook_cmd
import codepilot.commands.tasks as tasks_cmd
import codepilot.commands.providers as providers_cmd


@click.group()
@click.version_option(version=__version__)
@click.option("--json", "json_mode", is_flag=True, hidden=True,
              help="以 JSON 格式输出（全局选项）")
@click.pass_context
def main(ctx, json_mode):
    """CodePilot - 全局编程工作流自动化工具."""
    ctx.ensure_object(dict)
    ctx.obj["json_mode"] = json_mode
    if not json_mode:
        init_db()


main.add_command(init_cmd.init_)
main.add_command(status_cmd.status)
main.add_command(add_cmd.add)
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
