"""Project version file helpers for CodePilot binary releases."""

from __future__ import annotations

import re
from pathlib import Path

from codepilot.binary_types import VERSION_PATTERN


def validate_version_string(version: str) -> str:
    """Validate and normalize a release version string."""
    normalized = (version or "").strip()
    if not normalized:
        raise RuntimeError("版本号不能为空。")
    if not VERSION_PATTERN.fullmatch(normalized):
        raise RuntimeError(
            "版本号格式不合法。"
            "只允许字母、数字、点号、下划线和连字符，且不能以分隔符开头或结尾。"
        )
    return normalized


def read_project_version(project_root: str | Path) -> str:
    """Read the current package version from pyproject.toml."""
    project_root = Path(project_root).resolve()
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        raise RuntimeError(f"没有找到版本文件：{pyproject}")
    text = pyproject.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    if not match:
        raise RuntimeError("无法从 pyproject.toml 读取当前版本号。")
    return match.group(1).strip()


def update_project_version(project_root: str | Path, version: str) -> tuple[str, str]:
    """Update the package version in both pyproject.toml and codepilot/__init__.py."""
    project_root = Path(project_root).resolve()
    normalized = validate_version_string(version)
    old_version = read_project_version(project_root)

    pyproject = project_root / "pyproject.toml"
    init_file = project_root / "codepilot" / "__init__.py"
    if not init_file.exists():
        raise RuntimeError(f"没有找到版本文件：{init_file}")

    pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace")
    pyproject_updated, pyproject_count = re.subn(
        r'(?m)^(version\s*=\s*")([^"]+)("\s*)$',
        rf'\g<1>{normalized}\g<3>',
        pyproject_text,
        count=1,
    )
    if pyproject_count != 1:
        raise RuntimeError("更新 pyproject.toml 版本号失败。")

    init_text = init_file.read_text(encoding="utf-8", errors="replace")
    init_updated, init_count = re.subn(
        r'(?m)^(__version__\s*=\s*")([^"]+)("\s*)$',
        rf'\g<1>{normalized}\g<3>',
        init_text,
        count=1,
    )
    if init_count != 1:
        raise RuntimeError("更新 codepilot/__init__.py 版本号失败。")

    pyproject.write_text(pyproject_updated, encoding="utf-8")
    init_file.write_text(init_updated, encoding="utf-8")
    return old_version, normalized


def restore_project_version(project_root: str | Path, version: str) -> str:
    """Restore project version files to a known version."""
    _, current = update_project_version(project_root, version)
    return current
