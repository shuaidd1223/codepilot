"""codepilot init 命令：注册项目到数据库，生成 AGENTS.toml."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from codepilot import config, db as db_module
from codepilot.db import register_project


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

    # 检查是否已注册
    existing = db_module.get_project(project_name)
    if existing:
        click.echo(f"[yellow]项目 '{project_name}' 已注册，路径: {existing['path']}[/yellow]")
        if not no_config:
            _update_config(path / "AGENTS.toml", project_name)
        return

    # 注册到数据库
    config_path = None if no_config else str((path / "AGENTS.toml").resolve())
    register_project(
        name=project_name,
        path=project_path,
        base_branch="dev",
        default_mode="dual",
        config_file=config_path,
    )

    click.echo(f"[green]+ 项目 '{project_name}' 注册成功[/green]")
    click.echo(f"  路径: {project_path}")

    if not no_config:
        _update_config(path / "AGENTS.toml", project_name)
        click.echo(f"  配置: {(path / 'AGENTS.toml').resolve()}")


def _update_config(config_file: Path, project_name: str) -> None:
    """生成或更新 AGENTS.toml 配置文件."""
    config_file = config_file.resolve()
    if config_file.exists():
        click.echo(f"[dim]  已存在 AGENTS.toml，跳过生成[/dim]")
        return

    content = config.DEFAULT_TEMPLATE.format(name=project_name)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(content, encoding="utf-8")
