"""codepilot init 命令：注册项目到数据库，生成 AGENTS.toml."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from codepilot import config, db as db_module
from codepilot.db import register_project
from codepilot.output import echo


@click.command("init")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--name", "-n", "project_name", help="项目名称（默认取目录名）")
@click.option("--no-config", is_flag=True, help="不生成 AGENTS.toml，只注册到数据库")
def init_(
    path: Path,
    project_name: str | None,
    no_config: bool,
):
    """
    初始化项目：注册到数据库 + 生成 AGENTS.toml.

    PATH: 项目根目录路径.
    """
    db_module.init_db()

    project_path = str(path.resolve())
    project_name = project_name or path.name

    # 先按路径查——同一目录可能早以其他名字注册过
    existing = db_module.find_project_by_path(project_path)
    if existing and Path(existing["path"]).resolve() != path.resolve():
        existing = None  # 仅允许精确匹配（同路径），不让父级项目误命中
    if not existing:
        existing = db_module.get_project(project_name)

    if existing:
        echo(f"[yellow]项目已注册[/yellow]")
        click.echo(f"  名称:   {existing['name']}")
        click.echo(f"  路径:   {existing['path']}")
        click.echo(f"  分支:   {existing.get('base_branch') or '-'}")
        click.echo(f"  模式:   {existing.get('default_mode') or '-'}")
        click.echo(f"  配置:   {existing.get('config_file') or '-'}")
        stats = db_module.get_task_stats(existing["name"])
        click.echo(
            f"  任务:   {stats['total']} 总 / "
            f"{stats['backlog']} backlog / "
            f"{stats['in_progress']} 进行 / "
            f"{stats['done']} 完成"
        )
        if existing["name"] != project_name:
            echo(f"[dim]  (传入名称 '{project_name}' 被忽略，保留原名 '{existing['name']}' 以维护任务关联)[/dim]")
        if not no_config:
            _update_config(path / "AGENTS.toml", existing["name"])
        return

    # 注册到数据库
    config_path = None if no_config else str((path / "AGENTS.toml").resolve())
    register_project(
        name=project_name,
        path=project_path,
        base_branch="dev",
        default_mode="codex",
        config_file=config_path,
    )

    echo(f"[green]+ 项目 '{project_name}' 注册成功[/green]")
    click.echo(f"  路径: {project_path}")

    if not no_config:
        _update_config(path / "AGENTS.toml", project_name)
        click.echo(f"  配置: {(path / 'AGENTS.toml').resolve()}")


def _update_config(config_file: Path, project_name: str) -> None:
    """生成或更新 AGENTS.toml 配置文件."""
    config_file = config_file.resolve()
    if config_file.exists():
        echo(f"[dim]  已存在 AGENTS.toml，跳过生成[/dim]")
        return

    content = config.DEFAULT_TEMPLATE.format(name=project_name)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(content, encoding="utf-8")
