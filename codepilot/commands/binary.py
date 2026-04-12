"""Binary build and installation commands."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.binary import (
    build_binary,
    default_install_dir,
    install_binary,
    resolve_install_source,
)
from codepilot.output import echo


@click.group("binary")
def binary():
    """构建并安装当前平台的 CodePilot 二进制文件。"""


@binary.command("build")
@click.option("--output-dir", type=click.Path(file_okay=False, dir_okay=True, path_type=Path), default=None, help="产物输出目录，默认 dist/binary/<platform>")
@click.option("--name", default="codepilot", help="二进制文件名")
@click.option("--clean/--no-clean", default=True, help="构建前清理 PyInstaller 缓存")
@click.option("--install/--no-install", "install_after_build", default=False, help="构建完成后直接安装到用户 PATH 目录")
@click.option("--target-dir", type=click.Path(file_okay=False, dir_okay=True, path_type=Path), default=None, help="安装目录，默认按系统自动选择")
@click.option("--register-path/--no-register-path", default=True, help="安装后是否自动注册到用户 PATH")
def binary_build(
    output_dir: Path | None,
    name: str,
    clean: bool,
    install_after_build: bool,
    target_dir: Path | None,
    register_path: bool,
):
    """使用 PyInstaller 为当前操作系统构建单文件二进制。"""
    project_root = Path.cwd()
    try:
        result = build_binary(project_root=project_root, output_dir=output_dir, name=name, clean=clean)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    echo(f"[green][OK] 构建完成[/green]  {result.binary_path}")
    click.echo(f"  平台: {result.platform_tag}")
    click.echo(f"  输出目录: {result.dist_dir}")

    if not install_after_build:
        click.echo("\n下一步：")
        click.echo(f"  codepilot binary install --binary \"{result.binary_path}\"")
        click.echo("  注意：Windows 和 Linux 需要分别在各自系统上原生构建。")
        return

    install_result = install_binary(
        binary_path=result.binary_path,
        target_dir=target_dir,
        name=name,
        register_path=register_path,
    )
    echo(f"[green][OK] 已安装[/green]  {install_result.installed_path}")
    click.echo(f"  {install_result.registration_message}")


@binary.command("install")
@click.option("--binary", "binary_path", type=click.Path(exists=False, dir_okay=False, path_type=Path), default=None, help="要安装的二进制文件；不指定则优先使用当前运行的二进制，其次使用最近一次构建产物")
@click.option("--target-dir", type=click.Path(file_okay=False, dir_okay=True, path_type=Path), default=None, help="安装目录，默认按系统自动选择")
@click.option("--name", default="codepilot", help="安装后的命令名")
@click.option("--register-path/--no-register-path", default=True, help="是否自动注册到用户 PATH")
def binary_install(binary_path: Path | None, target_dir: Path | None, name: str, register_path: bool):
    """把构建好的二进制安装到用户命令目录，并注册 PATH。"""
    project_root = Path.cwd()
    try:
        source = resolve_install_source(binary_path, project_root=project_root, name=name)
        result = install_binary(binary_path=source, target_dir=target_dir, name=name, register_path=register_path)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    echo(f"[green][OK] 已安装[/green]  {result.installed_path}")
    click.echo(f"  来源: {result.source_path}")
    click.echo(f"  目标目录: {result.target_dir}")
    click.echo(f"  {result.registration_message}")


@binary.command("where")
def binary_where():
    """显示默认的二进制安装目录。"""
    click.echo(str(default_install_dir()))
