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
    """Build a binary for the current OS using PyInstaller onedir mode.

    Onedir produces a directory containing the exe alongside all DLLs,
    so the installed binary starts instantly without per-run extraction.
    """
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

    # onedir: PyInstaller creates <dist_dir>/<name>/<name>.exe
    source_dir = dist_dir / name
    binary_path = source_dir / executable_name(name)
    if not binary_path.exists():
        raise RuntimeError(f"构建命令已完成，但没有找到产物文件：{binary_path}")

    return BuildResult(
        binary_path=binary_path.resolve(),
        dist_dir=dist_dir.resolve(),
        build_dir=build_dir.resolve(),
        platform_tag=current_platform_tag(),
        source_dir=source_dir.resolve(),
    )


def _mirror_onedir(source_dir: Path, target_dir: Path, name: str) -> Path:
    """Mirror a onedir build into *target_dir*. Returns the installed exe path."""
    exe_name = executable_name(name)
    target_exe = target_dir / exe_name
    target_dir.mkdir(parents=True, exist_ok=True)

    source_names = {p.name for p in source_dir.rglob("*") if p.is_file()}
    for existing in list(target_dir.iterdir()):
        if existing.is_dir():
            continue
        if existing.name not in source_names:
            existing.unlink(missing_ok=True)

    for item in source_dir.rglob("*"):
        if item.is_dir():
            (target_dir / item.relative_to(source_dir)).mkdir(parents=True, exist_ok=True)
        else:
            dest = target_dir / item.relative_to(source_dir)
            if dest.exists() and dest.stat().st_size == item.stat().st_size:
                continue
            shutil.copy2(item, dest)
    return target_exe


def _is_onedir_tree(path: Path) -> bool:
    """Return True if *path* looks like a PyInstaller onedir directory.

    PyInstaller onedir (--onedir) output consists of a top-level directory
    containing the executable alongside an ``_internal`` folder with all
    bundled dependencies.  Earlier PyInstaller versions placed files
    directly in the root; both layouts are detected.
    """
    if not path.is_dir():
        return False
    exe = path / executable_name("codepilot")
    internal = path / "_internal"
    if exe.is_file() and internal.is_dir():
        return True
    # Legacy layout: at least 5 loose files in the directory root.
    files = [p for p in path.iterdir() if p.is_file()]
    return len(files) >= 5


def install_binary(
    *,
    binary_path: str | Path,
    target_dir: str | Path | None = None,
    name: str = "codepilot",
    register_path: bool = True,
) -> InstallResult:
    """Mirror a onedir build tree into the user install directory."""
    source = Path(binary_path).expanduser().resolve()
    if source.is_dir():
        source_dir = source
    elif _is_onedir_tree(source.parent):
        source_dir = source.parent
    else:
        # Standalone file — copy it directly (legacy onefile, or test).
        source_dir = None
    exe_name = executable_name(name)

    destination_dir = Path(target_dir).expanduser().resolve() if target_dir else default_install_dir()
    if source_dir is not None:
        destination = _mirror_onedir(source_dir, destination_dir, name)
        source_exe = source_dir / exe_name
    else:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / exe_name
        if source != destination:
            shutil.copy2(source, destination)
        source_exe = source

    if platform.system().lower() != "windows":
        destination.chmod(destination.stat().st_mode | 0o755)

    if register_path:
        path_registered, registration_message = register_install_dir(destination_dir)
    else:
        path_registered, registration_message = False, f"已安装到 `{destination}`。未修改 PATH。"

    return InstallResult(
        source_path=source_exe,
        installed_path=destination,
        target_dir=destination_dir,
        path_registered=path_registered,
        registration_message=registration_message,
    )


def _write_cli_wrapper(target_dir: Path, family: str) -> Path | None:
    """Create a ``cp-<family>`` wrapper in *target_dir*.

    On Windows a ``.cmd`` batch file is written; on POSIX a shell script.
    If the bundled vendor binary exists (e.g. ``vendor/opencode.exe``) the
    wrapper forwards to it; otherwise it falls back to the system command.
    Returns the path of the created wrapper, or ``None`` if it already existed.
    """
    if platform.system().lower() == "windows":
        wrapper_name = f"cp-{family}.cmd"
    else:
        wrapper_name = f"cp-{family}"
    wrapper_path = target_dir / wrapper_name
    if wrapper_path.exists():
        return None

    # CodePilot MUST use its own bundled opencode so it never conflicts
    # with the user's system-level installation.  If the vendor binary is
    # missing the installation is incomplete — skip wrapper creation.
    bundled = target_dir / "vendor" / executable_name(family)
    if not bundled.exists():
        return None

    command = str(bundled.resolve())
    if platform.system().lower() == "windows":
        wrapper_path.write_text(
            f"@echo off\r\n\"{command}\" %*\r\n",
            encoding="ascii",
        )
    else:
        wrapper_path.write_text(
            f"#!/usr/bin/env sh\n\nexec \"{command}\" \"$@\"\n",
            encoding="ascii",
        )
        wrapper_path.chmod(0o755)
    return wrapper_path


def ensure_cli_wrappers(target_dir: Path | None = None) -> list[Path]:
    """Create ``cp-opencode`` (and similar) wrappers in the install directory.

    These wrappers forward to the real CLI commands so that CodePilot's
    configuration can reference dedicated command names without conflicting
    with other installations of the same tools.
    """
    families = ["opencode"]
    install_dir = Path(target_dir).expanduser().resolve() if target_dir else default_install_dir()
    created: list[Path] = []
    for family in families:
        wrapper = _write_cli_wrapper(install_dir, family)
        if wrapper:
            created.append(wrapper)
    return created


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
    "ensure_cli_wrappers",
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
