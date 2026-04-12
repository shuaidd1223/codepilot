"""Top-level release command aliases."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.commands import binary as binary_cmd


@click.group("release")
def release():
    """版本发布入口：准备、校验和整理可交付产物。"""


@release.command("prepare")
@click.option("--version", "target_version", required=True, help="目标版本号，例如 0.1.1")
@click.option("--artifact", "artifacts", multiple=True, help="额外加入发布目录的其他平台二进制，格式: 平台=路径")
@click.option("--name", default="codepilot", help="发布包名称")
@click.option("--clean/--no-clean", default=True, help="构建和发布前先清理旧产物目录")
@click.option("--build-current/--no-build-current", default=True, help="自动构建当前平台二进制")
@click.option("--verify/--no-verify", default=True, help="发布完成后自动校验发布目录")
@click.pass_context
def release_prepare(
    ctx: click.Context,
    target_version: str,
    artifacts: tuple[str, ...],
    name: str,
    clean: bool,
    build_current: bool,
    verify: bool,
):
    """等价于 `codepilot binary prepare`。"""
    return ctx.invoke(
        binary_cmd.binary_prepare,
        target_version=target_version,
        artifacts=artifacts,
        name=name,
        clean=clean,
        build_current=build_current,
        verify=verify,
    )


@release.command("verify")
@click.option("--release-dir", type=click.Path(exists=False, file_okay=False, dir_okay=True, path_type=Path), default=None, help="要校验的发布目录；不指定则使用最新一次发布")
@click.pass_context
def release_verify(ctx: click.Context, release_dir: Path | None):
    """等价于 `codepilot binary verify`。"""
    return ctx.invoke(binary_cmd.binary_verify, release_dir=release_dir)


@release.command("bundle")
@click.option("--artifact", "artifacts", multiple=True, help="要打包到发布目录的二进制，格式: 平台=路径")
@click.option("--output-dir", type=click.Path(file_okay=False, dir_okay=True, path_type=Path), default=None, help="发布目录，默认 dist/release/codepilot-<version>")
@click.option("--version", default=None, help="发布版本号，默认读取当前 package 版本")
@click.option("--name", default="codepilot", help="发布包名称")
@click.option("--build-current/--no-build-current", default=False, help="发布前先原生构建当前平台二进制")
@click.option("--clean/--no-clean", default=True, help="重新生成发布目录前先清空旧目录")
@click.pass_context
def release_bundle(
    ctx: click.Context,
    artifacts: tuple[str, ...],
    output_dir: Path | None,
    version: str | None,
    name: str,
    build_current: bool,
    clean: bool,
):
    """等价于 `codepilot binary release`。"""
    return ctx.invoke(
        binary_cmd.binary_release,
        artifacts=artifacts,
        output_dir=output_dir,
        version=version,
        name=name,
        build_current=build_current,
        clean=clean,
    )
