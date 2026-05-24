"""codepilot init 命令：注册项目到数据库，生成 AGENTS.toml."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.core import config
from codepilot.core.gitignore import ensure_gitignore_entry
from codepilot.storage import database as db_module
from codepilot.storage.database import register_project
from codepilot.core.output import echo


def initialize_project(path: Path, project_name: str | None = None, *, no_config: bool = False) -> dict:
    """Register *path* as a project and optionally create AGENTS.toml."""
    db_module.init_db()

    try:
        resolved_path = path.resolve()
    except OSError as exc:
        raise RuntimeError(f"项目路径无效：{path}") from exc
    if not resolved_path.exists() or not resolved_path.is_dir():
        raise RuntimeError(f"项目路径不存在或不是目录：{resolved_path}")

    requested_name = project_name or resolved_path.name
    if not requested_name:
        raise RuntimeError("项目名称不能为空。")

    project_path = str(resolved_path)

    # 先按路径查——同一目录可能早以其他名字注册过
    existing = db_module.find_project_by_path(project_path)
    if existing and Path(existing["path"]).resolve() != resolved_path:
        existing = None  # 仅允许精确匹配（同路径），不让父级项目误命中
    if not existing:
        existing = db_module.get_project(requested_name)

    config_file = resolved_path / "AGENTS.toml"
    if existing:
        if not no_config:
            _update_config(config_file, existing["name"])
            ensure_gitignore_entry(resolved_path, config.CONFIG_FILENAME)
        return {
            "created": False,
            "project": existing,
            "requested_name": requested_name,
            "config_file": str(config_file.resolve()) if not no_config else "",
        }

    config_path = None if no_config else str(config_file.resolve())
    project = register_project(
        name=requested_name,
        path=project_path,
        base_branch="dev",
        default_mode="dual",
        config_file=config_path,
    )
    if not no_config:
        _update_config(config_file, requested_name)
        ensure_gitignore_entry(resolved_path, config.CONFIG_FILENAME)
    return {
        "created": True,
        "project": project,
        "requested_name": requested_name,
        "config_file": str(config_file.resolve()) if not no_config else "",
    }


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
    try:
        result = initialize_project(path, project_name, no_config=no_config)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    project = result["project"]
    project_name = result["requested_name"]
    if not result["created"]:
        echo("[yellow]项目已注册[/yellow]")
        click.echo(f"  名称:   {project['name']}")
        click.echo(f"  路径:   {project['path']}")
        click.echo(f"  分支:   {project.get('base_branch') or '-'}")
        click.echo(f"  模式:   {project.get('default_mode') or '-'}")
        click.echo(f"  配置:   {project.get('config_file') or '-'}")
        stats = db_module.get_task_stats(project["name"])
        click.echo(
            f"  任务:   {stats['total']} 总 / "
            f"{stats['backlog']} backlog / "
            f"{stats['in_progress']} 进行 / "
            f"{stats['done']} 完成"
        )
        if project["name"] != project_name:
            echo(f"[dim]  (传入名称 '{project_name}' 被忽略，保留原名 '{project['name']}' 以维护任务关联)[/dim]")
        return

    echo(f"[green]+ 项目 '{project_name}' 注册成功[/green]")
    click.echo(f"  路径: {project['path']}")

    if not no_config:
        click.echo(f"  配置: {result['config_file']}")


def _update_config(config_file: Path, project_name: str) -> None:
    """生成或更新 AGENTS.toml 配置文件."""
    config_file = config_file.resolve()
    if config_file.exists():
        echo("[dim]  已存在 AGENTS.toml，跳过生成[/dim]")
        return

    content = config.DEFAULT_TEMPLATE.format(name=project_name)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(content, encoding="utf-8")

