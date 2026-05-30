"""Platform-specific path resolution, discovery, and PATH-registration primitives."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

from codepilot import __version__


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


def default_release_dir(project_root: str | Path, version: str | None = None) -> Path:
    """Return the default release bundle directory."""
    release_version = (version or __version__).strip()
    return Path(project_root).resolve() / "dist" / "release" / f"codepilot-{release_version}"


def latest_release_dir(project_root: str | Path) -> Optional[Path]:
    """Return the newest release directory under dist/release."""
    root = Path(project_root).resolve() / "dist" / "release"
    if not root.exists():
        return None
    candidates = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0].resolve() if candidates else None


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


def _binary_candidates(root: Path, name: str) -> list[Path]:
    patterns = [f"**/{name}", f"**/{name}.exe"]
    results: list[Path] = []
    for pattern in patterns:
        results.extend(root.glob(pattern))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in results:
        resolved = str(candidate.resolve())
        if resolved in seen or not candidate.is_file():
            continue
        seen.add(resolved)
        unique.append(candidate.resolve())
    return unique


def list_built_binaries(project_root: str | Path, name: str = "codepilot") -> dict[str, Path]:
    """Return built binaries grouped by platform tag from dist/binary."""
    root = Path(project_root).resolve() / "dist" / "binary"
    if not root.exists():
        return {}

    artifacts: dict[str, Path] = {}
    for candidate in _binary_candidates(root, name):
        try:
            platform_tag = candidate.parent.relative_to(root).parts[0]
        except Exception:
            continue
        artifacts[platform_tag] = candidate
    return artifacts


def infer_platform_tag(binary_path: str | Path) -> str:
    """Infer a platform tag from a built binary location, falling back to the current host."""
    candidate = Path(binary_path).resolve()
    parts = list(candidate.parts)
    if "binary" in [part.lower() for part in parts]:
        lower_parts = [part.lower() for part in parts]
        idx = lower_parts.index("binary")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return current_platform_tag()


def _pyinstaller_data_arg(source: Path, dest: str) -> str:
    separator = ";" if platform.system().lower() == "windows" else ":"
    return f"{source}{separator}{dest}"


def _build_python_executable() -> str:
    override = os.environ.get("CODEPILOT_BUILD_PYTHON", "").strip()
    if override:
        return override

    if not getattr(sys, "frozen", False):
        return sys.executable

    candidates = ["python", "python3"]
    if platform.system().lower() == "windows":
        candidates.append("py")
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "python"


def _build_command(
    *,
    project_root: Path,
    dist_dir: Path,
    build_dir: Path,
    name: str,
    clean: bool,
) -> list[str]:
    cmd = [
        _build_python_executable(),
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
        "--collect-all",
        "lark_oapi",
        "--collect-all",
        "websockets",
    ]
    if clean:
        cmd.append("--clean")

    templates_dir = project_root / "codepilot" / "templates"
    if templates_dir.exists():
        cmd.extend(["--add-data", _pyinstaller_data_arg(templates_dir, "codepilot/templates")])

    web_dir = project_root / "codepilot" / "web"
    if web_dir.exists():
        cmd.extend(["--add-data", _pyinstaller_data_arg(web_dir, "codepilot/web")])

    cmd.append(str(project_root / "codepilot" / "__main__.py"))
    return cmd


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

        raw_entries = [entry for entry in str(current_path).split(os.pathsep) if entry.strip()]
        target_norm = _normalize_path(directory)
        filtered: list[str] = []
        seen: set[str] = set()
        for entry in raw_entries:
            normalized = _normalize_path(entry)
            if normalized == target_norm:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            filtered.append(entry)

        updated_entries = [str(directory), *filtered]
        if raw_entries == updated_entries:
            return False, f"`{directory}` 已经位于当前用户 PATH 前列。新开的终端可以直接使用 `codepilot`。"

        updated = os.pathsep.join(updated_entries)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, updated)

    _broadcast_windows_env_change()
    return True, f"已把 `{directory}` 置顶到当前用户 PATH。请重新打开终端后再使用 `codepilot`。"


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
