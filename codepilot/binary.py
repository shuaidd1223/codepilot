"""Binary build and installation helpers for CodePilot."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class BuildResult:
    """Result of a native binary build."""

    binary_path: Path
    dist_dir: Path
    build_dir: Path
    platform_tag: str


@dataclass
class InstallResult:
    """Result of installing a built binary into the user command path."""

    source_path: Path
    installed_path: Path
    target_dir: Path
    path_registered: bool
    registration_message: str


def _normalize_arch(raw: str) -> str:
    value = (raw or "").lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "aarch64": "arm64",
    }
    return aliases.get(value, value or "unknown")


def current_platform_tag() -> str:
    """Return a stable platform tag for binary artifact directories."""
    return f"{platform.system().lower()}-{_normalize_arch(platform.machine())}"


def executable_name(name: str = "codepilot", *, system: Optional[str] = None) -> str:
    """Return the platform-specific executable file name."""
    resolved_system = (system or platform.system()).lower()
    return f"{name}.exe" if resolved_system == "windows" else name


def default_dist_dir(project_root: str | Path) -> Path:
    """Return the default distribution directory for the current platform."""
    return Path(project_root).resolve() / "dist" / "binary" / current_platform_tag()


def default_build_dir(project_root: str | Path) -> Path:
    """Return the default PyInstaller work directory for the current platform."""
    return Path(project_root).resolve() / "build" / "pyinstaller" / current_platform_tag()


def default_install_dir() -> Path:
    """Return the user-local install directory that should contain the final command."""
    override = os.environ.get("CODEPILOT_INSTALL_DIR")
    if override:
        return Path(override).expanduser().resolve()

    system = platform.system().lower()
    if system == "windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return (local_app_data / "Programs" / "CodePilot" / "bin").resolve()
    return (Path.home() / ".local" / "bin").resolve()


def running_binary_path() -> Optional[Path]:
    """Return the current executable path when running as a frozen binary."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


def latest_built_binary(project_root: str | Path, name: str = "codepilot") -> Optional[Path]:
    """Return the most recently built binary artifact for this project, if any."""
    root = Path(project_root).resolve() / "dist" / "binary"
    if not root.exists():
        return None
    candidates = sorted(
        root.glob(f"**/{executable_name(name)}"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return candidates[0].resolve() if candidates else None


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


def _pyinstaller_data_arg(source: Path, dest: str) -> str:
    separator = ";" if platform.system().lower() == "windows" else ":"
    return f"{source}{separator}{dest}"


def _build_command(
    *,
    project_root: Path,
    dist_dir: Path,
    build_dir: Path,
    name: str,
    clean: bool,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name",
        name,
        "--paths",
        str(project_root),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir / "work"),
        "--specpath",
        str(build_dir / "spec"),
        "--collect-all",
        "codepilot",
        "--collect-all",
        "rich",
    ]
    if clean:
        cmd.append("--clean")

    templates_dir = project_root / "codepilot" / "templates"
    if templates_dir.exists():
        cmd.extend(["--add-data", _pyinstaller_data_arg(templates_dir, "codepilot/templates")])

    cmd.append(str(project_root / "codepilot" / "__main__.py"))
    return cmd


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
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    if result.returncode != 0:
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
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


def _normalize_path(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(value).expanduser())))


def path_contains(directory: str | Path, *, path_value: Optional[str] = None) -> bool:
    """Return whether a directory already exists in the given PATH value."""
    target = _normalize_path(directory)
    raw = path_value if path_value is not None else os.environ.get("PATH", "")
    return any(_normalize_path(entry) == target for entry in raw.split(os.pathsep) if entry.strip())


def _broadcast_windows_env_change() -> None:
    """Notify Windows shells that user environment variables changed."""
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_void_p()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def _register_windows_path(directory: Path) -> tuple[bool, str]:
    import winreg

    directory = directory.resolve()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        try:
            current_path, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path = ""

        if path_contains(directory, path_value=current_path):
            return False, f"`{directory}` 已经在当前用户 PATH 中。新开的终端可以直接使用 `codepilot`。"

        updated = current_path
        if updated and not updated.endswith(os.pathsep):
            updated += os.pathsep
        updated += str(directory)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, updated)

    _broadcast_windows_env_change()
    return True, f"已把 `{directory}` 写入当前用户 PATH。请重新打开终端后再使用 `codepilot`。"


def _preferred_posix_profile() -> Path:
    shell_name = Path(os.environ.get("SHELL", "")).name.lower()
    home = Path.home()
    if shell_name == "zsh":
        candidates = [home / ".zprofile", home / ".zshrc", home / ".profile"]
    elif shell_name == "bash":
        candidates = [home / ".bash_profile", home / ".bashrc", home / ".profile"]
    else:
        candidates = [home / ".profile"]

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _register_posix_path(directory: Path) -> tuple[bool, str]:
    directory = directory.resolve()
    if path_contains(directory):
        return False, f"`{directory}` 已经在当前 PATH 中，重新打开终端即可直接使用 `codepilot`。"

    profile = _preferred_posix_profile()
    profile.parent.mkdir(parents=True, exist_ok=True)
    block = (
        "\n# >>> codepilot >>>\n"
        f'export PATH="{directory}:$PATH"\n'
        "# <<< codepilot <<<\n"
    )
    existing = profile.read_text(encoding="utf-8", errors="replace") if profile.exists() else ""
    if "# >>> codepilot >>>" not in existing:
        with profile.open("a", encoding="utf-8") as handle:
            handle.write(block)
    return True, f"已把 `{directory}` 写入 `{profile}`。重新打开终端，或执行 `source {profile}` 后即可使用 `codepilot`。"


def register_install_dir(directory: str | Path) -> tuple[bool, str]:
    """Ensure the chosen install directory is available on the user's PATH."""
    target = Path(directory).expanduser().resolve()
    if platform.system().lower() == "windows":
        return _register_windows_path(target)
    return _register_posix_path(target)


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
        shutil.copy2(source, destination)
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
