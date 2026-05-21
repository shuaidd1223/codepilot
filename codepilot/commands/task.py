"""Task command group (canonical entrypoint for task lifecycle operations)."""

from __future__ import annotations

import click

from codepilot.commands import tasks as tasks_cmd
from codepilot.core.output import echo


@click.group("task")
def task_group() -> None:
    """任务管理入口：查看、检索、编辑、停止与重试任务。"""


task_group.add_command(tasks_cmd.show)
task_group.add_command(tasks_cmd.logs)
task_group.add_command(tasks_cmd.stop)
task_group.add_command(tasks_cmd.retry)
task_group.add_command(tasks_cmd.done)
task_group.add_command(tasks_cmd.cancel)
task_group.add_command(tasks_cmd.resume)
task_group.add_command(tasks_cmd.archive)
task_group.add_command(tasks_cmd.edit)
task_group.add_command(tasks_cmd.rm)
task_group.add_command(tasks_cmd.find)
task_group.add_command(tasks_cmd.sweep)


def _render_recovery_hints(task: dict) -> None:
    from codepilot.webapp.task_payloads import _derive_recovery_hints

    hints = _derive_recovery_hints(task)
    if hints:
        echo("[yellow]恢复建议[/yellow]")
        for hint in hints:
            click.echo(f"  - {hint}")
        click.echo()
