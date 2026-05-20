"""Binary build and installation commands."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.binary_support import vendor_fetcher
from codepilot.binary_support.manager import (
    build_binary,
    create_release_bundle,
    default_install_dir,
    install_binary,
    read_project_version,
    restore_project_version,
    resolve_release_dir,
    resolve_release_inputs,
    resolve_install_source,
    update_project_version,
    verify_release_bundle,
    _merge_release_inputs,
)
from codepilot.core.output import echo
from codepilot.core.paths import global_storage_root


@click.group("binary")
def binary():
    """构建并安装当前平台的 CodePilot 二进制文件。"""


@binary.command("build")
@click.option("--output-dir", type=click.Path(file_okay=False, dir_okay=True, path_type=Path), default=None, help="产物输出目录，默认 dist/binary/<platform>")
@click.option("--name", default="codepilot", help="二进制文件名")
@click.option("--clean/--no-clean", default=True, help="构建前清理 PyInstaller 缓存")
@click.option("--bundle-cli", default=None, help="逗号分隔的 bundled CLI provider，仅支持 opencode,codex")
@click.option("--install/--no-install", "install_after_build", default=False, help="构建完成后直接安装到用户 PATH 目录")
@click.option("--target-dir", type=click.Path(file_okay=False, dir_okay=True, path_type=Path), default=None, help="安装目录，默认按系统自动选择")
@click.option("--register-path/--no-register-path", default=True, help="安装后是否自动注册到用户 PATH")
def binary_build(
    output_dir: Path | None,
    name: str,
    clean: bool,
    bundle_cli: str | None,
    install_after_build: bool,
    target_dir: Path | None,
    register_path: bool,
):
    """使用 PyInstaller 为当前操作系统构建单文件二进制。"""
    project_root = Path.cwd()
    try:
        bundle_providers = vendor_fetcher.parse_bundle_cli_list(bundle_cli)
        result = build_binary(project_root=project_root, output_dir=output_dir, name=name, clean=clean)
        bundle_result = None
        if bundle_providers:
            bundle_result = vendor_fetcher.bundle_vendor_clis(
                bundle_providers,
                output_dir=result.dist_dir,
                platform_tag=result.platform_tag,
                cache_dir=result.build_dir / "vendor-cache",
            )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    echo(f"[green][OK] 构建完成[/green]  {result.binary_path}")
    click.echo(f"  平台: {result.platform_tag}")
    click.echo(f"  输出目录: {result.dist_dir}")
    if bundle_result:
        providers = ", ".join(entry.provider for entry in bundle_result.providers)
        click.echo(f"  bundled CLI: {providers}")
        click.echo(f"  vendor manifest: {bundle_result.manifest_path}")

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
    bundled_installed = vendor_fetcher.install_bundled_vendor(result.binary_path, target_dir=install_result.target_dir)
    echo(f"[green][OK] 已安装[/green]  {install_result.installed_path}")
    click.echo(f"  {install_result.registration_message}")
    if bundled_installed:
        click.echo(f"  bundled CLI: {install_result.target_dir / 'vendor'}")


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
        bundled_installed = vendor_fetcher.install_bundled_vendor(source, target_dir=result.target_dir)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    echo(f"[green][OK] 已安装[/green]  {result.installed_path}")
    click.echo(f"  来源: {result.source_path}")
    click.echo(f"  目标目录: {result.target_dir}")
    click.echo(f"  {result.registration_message}")
    if bundled_installed:
        click.echo(f"  bundled CLI: {result.target_dir / 'vendor'}")


@binary.command("where")
def binary_where():
    """显示默认的二进制安装目录。"""
    click.echo(str(default_install_dir()))


@binary.command("release")
@click.option(
    "--artifact",
    "artifacts",
    multiple=True,
    help="要打包到发布目录的二进制，格式: 平台=路径，例如 windows-x86_64=dist/binary/windows-x86_64/codepilot.exe",
)
@click.option("--output-dir", type=click.Path(file_okay=False, dir_okay=True, path_type=Path), default=None, help="发布目录，默认 dist/release/codepilot-<version>")
@click.option("--version", default=None, help="发布版本号，默认读取当前 package 版本")
@click.option("--name", default="codepilot", help="发布包名称")
@click.option("--build-current/--no-build-current", default=False, help="发布前先原生构建当前平台二进制")
@click.option("--clean/--no-clean", default=True, help="重新生成发布目录前先清空旧目录")
def binary_release(
    artifacts: tuple[str, ...],
    output_dir: Path | None,
    version: str | None,
    name: str,
    build_current: bool,
    clean: bool,
):
    """整理已构建产物为标准发布目录，并生成 zip 和 SHA256。"""
    project_root = Path.cwd()
    try:
        resolved = []
        if artifacts:
            resolved = resolve_release_inputs(project_root=project_root, name=name, artifact_specs=list(artifacts))
        else:
            try:
                resolved = resolve_release_inputs(project_root=project_root, name=name, artifact_specs=[])
            except RuntimeError:
                resolved = []
        if build_current:
            built = build_binary(project_root=project_root, name=name, clean=clean)
            resolved = _merge_release_inputs(resolved, (built.platform_tag, built.binary_path))
        if not resolved:
            raise RuntimeError(
                "当前没有可发布的二进制产物。"
                "请先运行 `codepilot binary build`，或改用 `codepilot binary release --build-current`。"
            )
        result = create_release_bundle(
            project_root=project_root,
            artifacts=resolved,
            output_dir=output_dir,
            version=version,
            name=name,
            clean=clean,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    echo(f"[green][OK] 发布目录已生成[/green]  {result.release_dir}")
    click.echo(f"  manifest: {result.manifest_path}")
    click.echo(f"  checksums: {result.checksum_path}")
    click.echo(f"  guide: {result.guide_path}")
    click.echo(f"  ai guide: {result.ai_guide_path}")
    click.echo(f"  ai manifest: {result.ai_manifest_path}")
    click.echo(f"  summary: {result.summary_path}")
    for artifact in result.artifacts:
        click.echo(f"  - {artifact.platform_tag}: {artifact.archive_path.name} ({artifact.archive_format})")


@binary.command("verify")
@click.option("--release-dir", type=click.Path(exists=False, file_okay=False, dir_okay=True, path_type=Path), default=None, help="要校验的发布目录；不指定则使用最新一次发布")
def binary_verify(release_dir: Path | None):
    """校验发布目录中的清单、校验值和文件完整性。"""
    project_root = Path.cwd()
    try:
        target = resolve_release_dir(project_root, release_dir)
        result = verify_release_bundle(target)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    if result.issues:
        echo(f"[red][X] 发布目录校验失败[/red]  {result.release_dir}")
        for issue in result.issues:
            click.echo(f"  - {issue}")
        raise click.ClickException("发布目录存在校验问题。")

    echo(f"[green][OK] 发布目录校验通过[/green]  {result.release_dir}")
    click.echo(f"  manifest: {result.manifest_path}")
    click.echo(f"  checksums: {result.checksum_path}")
    click.echo(f"  checked_files: {result.checked_files}")


@binary.command("prepare")
@click.option("--version", "target_version", required=True, help="目标版本号，例如 0.7.4")
@click.option(
    "--artifact",
    "artifacts",
    multiple=True,
    help="额外加入发布目录的其他平台二进制，格式: 平台=路径",
)
@click.option("--name", default="codepilot", help="发布包名称")
@click.option("--clean/--no-clean", default=True, help="构建和发布前先清理旧产物目录")
@click.option("--build-current/--no-build-current", default=True, help="自动构建当前平台二进制")
@click.option("--verify/--no-verify", default=True, help="发布完成后自动校验发布目录")
def binary_prepare(
    target_version: str,
    artifacts: tuple[str, ...],
    name: str,
    clean: bool,
    build_current: bool,
    verify: bool,
):
    """同步版本号并准备一个可交付的本地发布目录。"""
    project_root = Path.cwd()
    previous = ""
    current = ""
    version_updated = False
    try:
        previous, current = update_project_version(project_root, target_version)
        version_updated = True
        resolved = []
        if artifacts:
            resolved = resolve_release_inputs(project_root=project_root, name=name, artifact_specs=list(artifacts))
        if build_current:
            built = build_binary(project_root=project_root, name=name, clean=clean)
            resolved = _merge_release_inputs(resolved, (built.platform_tag, built.binary_path))
        if not resolved:
            resolved = resolve_release_inputs(project_root=project_root, name=name, artifact_specs=[])
        release = create_release_bundle(
            project_root=project_root,
            artifacts=resolved,
            version=current,
            name=name,
            clean=clean,
        )
        verification = verify_release_bundle(release.release_dir) if verify else None
    except RuntimeError as exc:
        if version_updated and previous:
            try:
                restore_project_version(project_root, previous)
            except RuntimeError:
                pass
        raise click.ClickException(str(exc)) from exc

    echo(f"[green][OK] 版本已更新[/green]  {previous} -> {current}")
    echo(f"[green][OK] 发布目录已准备好[/green]  {release.release_dir}")
    click.echo(f"  manifest: {release.manifest_path}")
    click.echo(f"  checksums: {release.checksum_path}")
    click.echo(f"  guide: {release.guide_path}")
    click.echo(f"  ai guide: {release.ai_guide_path}")
    click.echo(f"  ai manifest: {release.ai_manifest_path}")
    click.echo(f"  summary: {release.summary_path}")
    for artifact in release.artifacts:
        click.echo(f"  - {artifact.platform_tag}: {artifact.archive_path.name} ({artifact.archive_format})")

    if verification:
        if verification.issues:
            echo(f"[red][X] 发布目录校验失败[/red]  {verification.release_dir}")
            for issue in verification.issues:
                click.echo(f"  - {issue}")
            raise click.ClickException("版本已更新，但发布目录校验失败。")
        echo(f"[green][OK] 发布目录校验通过[/green]  {verification.release_dir}")
        click.echo(f"  checked_files: {verification.checked_files}")
