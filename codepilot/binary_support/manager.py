"""Binary build and installation helpers for CodePilot.

Implementation is split across four focused modules:

- :mod:`codepilot.binary_types` -- result dataclasses and constants.
- :mod:`codepilot.binary_paths` -- platform/path discovery and PATH-registration
  primitives.
- :mod:`codepilot.binary_version` -- pyproject/``__init__.py`` version helpers.
- :mod:`codepilot.binary_release` -- release bundle creation and verification.

This shell re-exports the public API for backwards compatibility. Legacy
private helper names remain available as module attributes through explicit
re-export aliases, but are intentionally omitted from ``__all__``. The three
orchestrator functions (:func:`build_binary`, :func:`install_binary`,
:func:`resolve_install_source`) remain defined here so that tests which
monkeypatch their shell-module-level dependencies (e.g. ``default_build_dir``,
``register_install_dir``, ``running_binary_path``) continue to work.
"""

from __future__ import annotations

# Re-exported standard-library modules for test monkeypatching.
import os
import platform
import shutil
import subprocess
from pathlib import Path

from codepilot.core.text_decode import decode_subprocess_text
from codepilot.binary_support.paths import (
    _binary_candidates as _binary_candidates,
    _broadcast_windows_env_change as _broadcast_windows_env_change,
    _build_command,
    _normalize_arch as _normalize_arch,
    _normalize_path as _normalize_path,
    _preferred_posix_profile as _preferred_posix_profile,
    _pyinstaller_data_arg as _pyinstaller_data_arg,
    _register_posix_path as _register_posix_path,
    _register_windows_path as _register_windows_path,
    current_platform_tag,
    default_build_dir,
    default_dist_dir,
    default_install_dir,
    default_release_dir,
    executable_name,
    infer_platform_tag,
    latest_built_binary,
    latest_release_dir,
    list_built_binaries,
    path_contains,
    register_install_dir,
    running_binary_path,
)
from codepilot.binary_support.release import (
    _archive_name as _archive_name,
    _archive_root_folder as _archive_root_folder,
    _merge_release_inputs as _merge_release_inputs,
    _posix_install_script as _posix_install_script,
    _read_archive_members as _read_archive_members,
    _release_guide_text as _release_guide_text,
    _release_script_name as _release_script_name,
    _release_script_text as _release_script_text,
    _release_summary_text as _release_summary_text,
    _sha256_file as _sha256_file,
    _windows_install_script as _windows_install_script,
    _write_release_archive as _write_release_archive,
    create_release_bundle,
    resolve_release_dir,
    resolve_release_inputs,
    verify_release_bundle,
)
from codepilot.binary_support.types import (
    VERSION_PATTERN,
    BuildResult,
    InstallResult,
    ReleaseArtifact,
    ReleaseResult,
    VerificationResult,
)
from codepilot.binary_support.version import (
    read_project_version,
    restore_project_version,
    update_project_version,
    validate_version_string,
)


def resolve_install_source(binary: str | Path | None, *, project_root: str | Path, name: str = "codepilot") -> Path:
    """Resolve which binary should be installed."""
    if binary:
        candidate = Path(binary).expanduser()
        if not candidate.exists():
            raise RuntimeError(f"没有找到要安装的二进制文件: {candidate}")
        return candidate.resolve()

    current = running_binary_path()
    if current:
        return current

    latest = latest_built_binary(project_root, name=name)
    if latest:
        return latest

    raise RuntimeError(
        "当前没有找到可安装的二进制文件。"
        "请先运行 `codepilot binary build`，或者通过 `--binary PATH` 显式指定产物文件。"
    )


def build_binary(
    *,
    project_root: str | Path,
    output_dir: str | Path | None = None,
    name: str = "codepilot",
    clean: bool = True,
) -> BuildResult:
    """Build a single-file binary for the current OS using PyInstaller."""
    root = Path(project_root).resolve()
    entrypoint = root / "codepilot" / "__main__.py"
    if not entrypoint.exists():
        raise RuntimeError("当前目录不是 CodePilot 源码根目录，缺少 `codepilot/__main__.py`。")

    dist_dir = Path(output_dir).expanduser().resolve() if output_dir else default_dist_dir(root)
    build_dir = default_build_dir(root)
    dist_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    cmd = _build_command(project_root=root, dist_dir=dist_dir, build_dir=build_dir, name=name, clean=clean)
    result = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=False,
        timeout=1800,
    )
    if result.returncode != 0:
        output = (decode_subprocess_text(result.stdout) + "\n" + decode_subprocess_text(result.stderr)).strip()
        hint = (
            "当前无法构建二进制。"
            "请先确认已经安装 PyInstaller：`pip install .[build]` 或 `pip install pyinstaller`。"
        )
        tail = "\n".join(output.splitlines()[-20:]).strip()
        if tail:
            raise RuntimeError(f"{hint}\n\nPyInstaller 输出:\n{tail}")
        raise RuntimeError(hint)

    binary_path = dist_dir / executable_name(name)
    if not binary_path.exists():
        raise RuntimeError(f"构建命令已完成，但没有找到产物文件：{binary_path}")

    return BuildResult(
        binary_path=binary_path.resolve(),
        dist_dir=dist_dir.resolve(),
        build_dir=build_dir.resolve(),
        platform_tag=current_platform_tag(),
    )


def _copy_or_replace(source: Path, destination: Path) -> None:
    """Copy *source* to *destination*, tolerating locked files on Windows."""
    try:
        shutil.copy2(source, destination)
    except PermissionError:
        if platform.system().lower() != "windows":
            raise
        # Destination file may be locked (e.g. a running instance).
        # On Windows we can rename a locked file, then copy the new one in.
        backup = destination.with_name(destination.stem + ".old" + destination.suffix)
        try:
            os.replace(destination, backup)
        except OSError:
            # Cannot even rename — give up on copying, the existing binary
            # is likely still usable.
            return
        try:
            shutil.copy2(source, destination)
        except PermissionError:
            # Last resort: try to restore the backup
            try:
                os.replace(backup, destination)
            except OSError:
                pass
            raise


def install_binary(
    *,
    binary_path: str | Path,
    target_dir: str | Path | None = None,
    name: str = "codepilot",
    register_path: bool = True,
) -> InstallResult:
    """Copy a built binary into the user install directory and optionally register PATH."""
    source = Path(binary_path).expanduser().resolve()
    if not source.exists():
        raise RuntimeError(f"没有找到二进制文件：{source}")

    destination_dir = Path(target_dir).expanduser().resolve() if target_dir else default_install_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / executable_name(name)

    if source != destination:
        _copy_or_replace(source, destination)
    if platform.system().lower() != "windows":
        destination.chmod(destination.stat().st_mode | 0o755)

    path_registered = False
    registration_message = ""
    if register_path:
        path_registered, registration_message = register_install_dir(destination_dir)
    else:
        registration_message = f"已安装到 `{destination}`。未修改 PATH。"

    return InstallResult(
        source_path=source,
        installed_path=destination,
        target_dir=destination_dir,
        path_registered=path_registered,
        registration_message=registration_message,
    )


def ensure_global_config() -> Path | None:
    """Create the global AGENTS.toml if it does not already exist.

    Called automatically after binary installation so that users can start
    using CodePilot without having to run :command:`codepilot config init`
    manually.  When a global config already exists (e.g. from a previous
    installation) this function is a no-op.
    """
    from codepilot.core.config import find_global_config, resolve_global_config_path

    existing = find_global_config()
    if existing is not None:
        return None

    from codepilot.commands.config_cmd import _canonical_config, render_agents_toml, ensure_secrets_template

    config_path = resolve_global_config_path()
    canonical = _canonical_config({}, project_name=config_path.parent.name)
    content = render_agents_toml(canonical)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")
    ensure_secrets_template(config_path.parent)
    return config_path


__all__ = [
    "BuildResult",
    "InstallResult",
    "ReleaseArtifact",
    "ReleaseResult",
    "VerificationResult",
    "VERSION_PATTERN",
    "build_binary",
    "create_release_bundle",
    "current_platform_tag",
    "default_build_dir",
    "ensure_global_config",
    "default_dist_dir",
    "default_install_dir",
    "default_release_dir",
    "executable_name",
    "infer_platform_tag",
    "install_binary",
    "latest_built_binary",
    "latest_release_dir",
    "list_built_binaries",
    "path_contains",
    "read_project_version",
    "register_install_dir",
    "resolve_install_source",
    "resolve_release_dir",
    "resolve_release_inputs",
    "restore_project_version",
    "running_binary_path",
    "update_project_version",
    "validate_version_string",
    "verify_release_bundle",
]
