"""Binary build and installation helpers for CodePilot."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from zipfile import ZIP_DEFLATED, ZipFile

from codepilot import __version__
from codepilot.agent_support import ai_guide_markdown, manifest_json


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


@dataclass
class ReleaseArtifact:
    """One packaged binary inside a release bundle."""

    platform_tag: str
    source_path: Path
    staged_path: Path
    archive_path: Path
    archive_format: str
    binary_sha256: str
    archive_sha256: str


@dataclass
class ReleaseResult:
    """Result of assembling a release directory."""

    release_dir: Path
    manifest_path: Path
    checksum_path: Path
    guide_path: Path
    summary_path: Path
    ai_guide_path: Path
    ai_manifest_path: Path
    artifacts: list[ReleaseArtifact]


@dataclass
class VerificationResult:
    """Result of verifying a release bundle."""

    release_dir: Path
    manifest_path: Path
    checksum_path: Path
    checked_files: int
    issues: list[str]


VERSION_PATTERN = re.compile(r"^[0-9A-Za-z]+(?:[0-9A-Za-z._-]*[0-9A-Za-z])?$")


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _release_guide_text(name: str, version: str, artifacts: list[tuple[str, Path]]) -> str:
    lines = [
        f"# {name} {version} 发布说明",
        "",
        "这个目录用于分发已经构建好的 CodePilot 二进制文件。",
        "",
        "## 包含内容",
        "",
        "- `release.json`: 发布元数据清单",
        "- `SHA256SUMS.txt`: 所有二进制和压缩包的校验值",
        "- `AI_MANIFEST.json`: 给其他 AI 的机器可读命令清单",
        "- `AI_USAGE.zh-CN.md`: 给其他 AI 的 Markdown 调用手册",
        "- `<platform>/`: 平台对应的原始二进制和安装脚本",
        "- Windows: `codepilot-<version>-<platform>.zip`",
        "- Linux: `codepilot-<version>-<platform>.tar.gz`",
        "",
        "## 安装方式",
        "",
        "### Windows",
        "",
        "1. 解压 `codepilot-<version>-windows-x86_64.zip`",
        "2. 双击运行 `install-codepilot.cmd`",
        "3. 重新打开终端后执行 `codepilot --help` 验证",
        "",
        "### Linux",
        "",
        "1. 解压 `codepilot-<version>-linux-x86_64.tar.gz`",
        "2. 执行 `chmod +x install-codepilot.sh codepilot`",
        "3. 执行 `./install-codepilot.sh`",
        "4. 重新打开终端后执行 `codepilot --help` 验证",
        "",
        "## 当前包含的平台",
        "",
    ]
    for platform_tag, source_path in artifacts:
        lines.append(f"- `{platform_tag}` -> `{source_path.name}`")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- Windows 和 Linux 需要分别在各自系统上原生构建，不能直接交叉复用。",
            "- 安装脚本默认安装到用户目录，不需要管理员权限。",
        ]
    )
    return "\n".join(lines) + "\n"


def _release_summary_text(name: str, version: str, artifacts: list[ReleaseArtifact]) -> str:
    lines = [
        f"# {name} {version} 发布摘要",
        "",
        f"- 版本: `{version}`",
        f"- 生成时间: `{datetime.now().isoformat()}`",
        f"- 产物数量: `{len(artifacts)}`",
        "",
        "## 产物列表",
        "",
    ]
    for artifact in artifacts:
        lines.extend(
            [
                f"### {artifact.platform_tag}",
                "",
                f"- 二进制: `{artifact.staged_path.name}`",
                f"- 压缩包: `{artifact.archive_path.name}`",
                f"- 压缩格式: `{artifact.archive_format}`",
                f"- 二进制 SHA256: `{artifact.binary_sha256}`",
                f"- 压缩包 SHA256: `{artifact.archive_sha256}`",
                "",
            ]
        )
    lines.extend(
        [
            "## 交付建议",
            "",
            "- 对外分发时优先发送压缩包，不要直接发送裸二进制。",
            "- 分发时附带 `README.zh-CN.md` 和 `SHA256SUMS.txt`。",
            "- 如果接收方是其他 AI 或自动化系统，优先读取 `AI_MANIFEST.json` 和 `AI_USAGE.zh-CN.md`。",
            "- 用户安装后可执行 `codepilot --help` 验证命令是否可用。",
        ]
    )
    return "\n".join(lines) + "\n"


def _windows_install_script(binary_name: str) -> str:
    return (
        "@echo off\n"
        "setlocal\n"
        "set SCRIPT_DIR=%~dp0\n"
        "set TARGET_DIR=%LOCALAPPDATA%\\Programs\\CodePilot\\bin\n"
        "if not exist \"%TARGET_DIR%\" mkdir \"%TARGET_DIR%\"\n"
        f"copy /Y \"%SCRIPT_DIR%{binary_name}\" \"%TARGET_DIR%\\{binary_name}\" >nul\n"
        "powershell -NoProfile -Command \"$dir=$env:LOCALAPPDATA + '\\Programs\\CodePilot\\bin';"
        "$current=[Environment]::GetEnvironmentVariable('Path','User');"
        "if(-not $current){$current=''};"
        "$parts=@($current -split ';' | Where-Object { $_ -ne '' });"
        "if($parts -notcontains $dir){"
        "$updated=if($current){$current + ';' + $dir}else{$dir};"
        "[Environment]::SetEnvironmentVariable('Path',$updated,'User')};"
        "\"\n"
        "echo CodePilot 已安装到 %TARGET_DIR%\n"
        "echo 请重新打开终端后再运行 codepilot --help\n"
        "endlocal\n"
    )


def _posix_install_script(binary_name: str) -> str:
    return (
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        'SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"\n'
        'TARGET_DIR="${HOME}/.local/bin"\n'
        'mkdir -p "${TARGET_DIR}"\n'
        f'cp "{binary_name}" "${{TARGET_DIR}}/{binary_name}"\n'
        f'chmod +x "${{TARGET_DIR}}/{binary_name}"\n'
        'PROFILE=""\n'
        'if [ -n "${SHELL:-}" ] && [ "$(basename "${SHELL}")" = "zsh" ]; then PROFILE="${HOME}/.zprofile"; fi\n'
        'if [ -z "${PROFILE}" ] && [ -f "${HOME}/.bash_profile" ]; then PROFILE="${HOME}/.bash_profile"; fi\n'
        'if [ -z "${PROFILE}" ] && [ -f "${HOME}/.bashrc" ]; then PROFILE="${HOME}/.bashrc"; fi\n'
        'if [ -z "${PROFILE}" ]; then PROFILE="${HOME}/.profile"; fi\n'
        'if ! printf "%s" "${PATH}" | tr ":" "\\n" | grep -Fx "${TARGET_DIR}" >/dev/null 2>&1; then\n'
        '  if [ ! -f "${PROFILE}" ] || ! grep -F \'# >>> codepilot >>>\' "${PROFILE}" >/dev/null 2>&1; then\n'
        '    {\n'
        '      printf "\\n# >>> codepilot >>>\\n"\n'
        '      printf \'export PATH="%s:$PATH"\\n\' "${TARGET_DIR}"\n'
        '      printf "# <<< codepilot <<<\\n"\n'
        '    } >> "${PROFILE}"\n'
        '  fi\n'
        'fi\n'
        'echo "CodePilot 已安装到 ${TARGET_DIR}"\n'
        'echo "重新打开终端，或执行 source ${PROFILE} 后再运行 codepilot --help"\n'
    )


def _release_script_name(platform_tag: str) -> str:
    return "install-codepilot.cmd" if platform_tag.lower().startswith("windows") else "install-codepilot.sh"


def _release_script_text(platform_tag: str, binary_name: str) -> str:
    return _windows_install_script(binary_name) if platform_tag.lower().startswith("windows") else _posix_install_script(binary_name)


def _archive_name(name: str, version: str, platform_tag: str) -> tuple[str, str]:
    """Return the archive filename and format for one platform."""
    if platform_tag.lower().startswith("windows"):
        return f"{name}-{version}-{platform_tag}.zip", "zip"
    return f"{name}-{version}-{platform_tag}.tar.gz", "tar.gz"


def _archive_root_folder(archive_path: Path, archive_format: str) -> str:
    if archive_format == "zip":
        return archive_path.name[:-4]
    if archive_format == "tar.gz":
        return archive_path.name[:-7]
    raise RuntimeError(f"不支持的压缩格式：{archive_format}")


def _write_release_archive(
    archive_path: Path,
    *,
    archive_format: str,
    folder_name: str,
    staged_path: Path,
    script_path: Path,
    guide_path: Path,
    ai_guide_path: Path,
    ai_manifest_path: Path,
) -> None:
    """Create the platform archive with binary, installer, and Chinese guide."""
    if archive_format == "zip":
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as bundle:
            bundle.write(staged_path, arcname=f"{folder_name}/{staged_path.name}")
            bundle.write(script_path, arcname=f"{folder_name}/{script_path.name}")
            bundle.write(guide_path, arcname=f"{folder_name}/README.zh-CN.md")
            bundle.write(ai_guide_path, arcname=f"{folder_name}/AI_USAGE.zh-CN.md")
            bundle.write(ai_manifest_path, arcname=f"{folder_name}/AI_MANIFEST.json")
        return

    if archive_format == "tar.gz":
        with tarfile.open(archive_path, "w:gz") as bundle:
            bundle.add(staged_path, arcname=f"{folder_name}/{staged_path.name}")
            bundle.add(script_path, arcname=f"{folder_name}/{script_path.name}")
            bundle.add(guide_path, arcname=f"{folder_name}/README.zh-CN.md")
            bundle.add(ai_guide_path, arcname=f"{folder_name}/AI_USAGE.zh-CN.md")
            bundle.add(ai_manifest_path, arcname=f"{folder_name}/AI_MANIFEST.json")
        return

    raise RuntimeError(f"不支持的压缩格式：{archive_format}")


def _read_archive_members(archive_path: Path, archive_format: str) -> set[str]:
    """Return the file entries stored in one archive."""
    if archive_format == "zip":
        with ZipFile(archive_path, "r") as bundle:
            return {name.rstrip("/") for name in bundle.namelist() if not name.endswith("/")}
    if archive_format == "tar.gz":
        with tarfile.open(archive_path, "r:gz") as bundle:
            return {member.name.rstrip("/") for member in bundle.getmembers() if member.isfile()}
    raise RuntimeError(f"不支持的压缩格式：{archive_format}")


def resolve_release_inputs(
    *,
    project_root: str | Path,
    name: str = "codepilot",
    artifact_specs: Optional[list[str]] = None,
) -> list[tuple[str, Path]]:
    """Resolve which binaries should be included in a release bundle."""
    resolved: list[tuple[str, Path]] = []
    specs = artifact_specs or []
    if specs:
        for spec in specs:
            raw = spec.strip()
            if not raw:
                continue
            if "=" in raw:
                platform_tag, raw_path = raw.split("=", 1)
                platform_tag = platform_tag.strip()
                binary_path = Path(raw_path.strip()).expanduser().resolve()
            else:
                binary_path = Path(raw).expanduser().resolve()
                platform_tag = infer_platform_tag(binary_path)
            if not binary_path.exists():
                raise RuntimeError(f"没有找到要发布的二进制文件：{binary_path}")
            resolved.append((platform_tag, binary_path))
        return resolved

    built = list_built_binaries(project_root, name=name)
    if built:
        return sorted(built.items())

    latest = latest_built_binary(project_root, name=name)
    if latest:
        return [(infer_platform_tag(latest), latest)]

    raise RuntimeError(
        "当前没有可发布的二进制产物。"
        "请先运行 `codepilot binary build`，或通过 `--artifact 平台=路径` 显式传入二进制文件。"
    )


def _merge_release_inputs(existing: list[tuple[str, Path]], extra: tuple[str, Path]) -> list[tuple[str, Path]]:
    """Merge one platform artifact into an existing artifact list, replacing same-platform entries."""
    merged = {platform_tag: path for platform_tag, path in existing}
    merged[extra[0]] = extra[1]
    return sorted(merged.items())


def create_release_bundle(
    *,
    project_root: str | Path,
    artifacts: list[tuple[str, str | Path]],
    output_dir: str | Path | None = None,
    version: str | None = None,
    name: str = "codepilot",
    clean: bool = True,
) -> ReleaseResult:
    """Create a release directory with staged binaries, archives, manifest, and checksums."""
    root = Path(project_root).resolve()
    release_version = (version or __version__).strip()
    release_dir = Path(output_dir).expanduser().resolve() if output_dir else default_release_dir(root, release_version)
    if clean and release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)
    normalized_artifacts = [(platform_tag, Path(source).expanduser().resolve()) for platform_tag, source in artifacts]

    packaged: list[ReleaseArtifact] = []
    manifest_payload = {
        "name": name,
        "version": release_version,
        "generated_at": datetime.now().isoformat(),
        "artifacts": [],
    }
    guide_path = release_dir / "README.zh-CN.md"
    guide_path.write_text(_release_guide_text(name, release_version, normalized_artifacts), encoding="utf-8")
    ai_guide_path = release_dir / "AI_USAGE.zh-CN.md"
    ai_guide_path.write_text(ai_guide_markdown(command_name=name), encoding="utf-8")
    ai_manifest_path = release_dir / "AI_MANIFEST.json"
    ai_manifest_path.write_text(
        manifest_json(version=release_version, command_name=name, binary_name=name),
        encoding="utf-8",
    )

    for platform_tag, source_path in normalized_artifacts:
        if not source_path.exists():
            raise RuntimeError(f"没有找到要打包的二进制文件：{source_path}")

        artifact_dir = release_dir / platform_tag
        artifact_dir.mkdir(parents=True, exist_ok=True)
        staged_name = source_path.name
        staged_path = artifact_dir / staged_name
        shutil.copy2(source_path, staged_path)
        script_name = _release_script_name(platform_tag)
        script_path = artifact_dir / script_name
        script_path.write_text(_release_script_text(platform_tag, staged_name), encoding="utf-8", newline="\n")
        if not platform_tag.lower().startswith("windows"):
            script_path.chmod(script_path.stat().st_mode | 0o755)

        archive_name, archive_format = _archive_name(name, release_version, platform_tag)
        archive_path = release_dir / archive_name
        folder_name = f"{name}-{release_version}-{platform_tag}"
        _write_release_archive(
            archive_path,
            archive_format=archive_format,
            folder_name=folder_name,
            staged_path=staged_path,
            script_path=script_path,
            guide_path=guide_path,
            ai_guide_path=ai_guide_path,
            ai_manifest_path=ai_manifest_path,
        )

        binary_sha = _sha256_file(staged_path)
        archive_sha = _sha256_file(archive_path)
        packaged.append(
            ReleaseArtifact(
                platform_tag=platform_tag,
                source_path=source_path,
                staged_path=staged_path,
                archive_path=archive_path,
                archive_format=archive_format,
                binary_sha256=binary_sha,
                archive_sha256=archive_sha,
            )
        )
        manifest_payload["artifacts"].append(
            {
                "platform": platform_tag,
                "source_path": str(source_path),
                "staged_path": str(staged_path),
                "archive_path": str(archive_path),
                "archive_format": archive_format,
                "install_script": str(script_path),
                "binary_sha256": binary_sha,
                "archive_sha256": archive_sha,
            }
        )

    checksum_path = release_dir / "SHA256SUMS.txt"
    checksum_lines: list[str] = []
    for artifact in packaged:
        checksum_lines.append(f"{artifact.binary_sha256}  {artifact.staged_path.relative_to(release_dir)}")
        checksum_lines.append(f"{artifact.archive_sha256}  {artifact.archive_path.relative_to(release_dir)}")
    checksum_path.write_text("\n".join(checksum_lines) + ("\n" if checksum_lines else ""), encoding="utf-8")

    summary_path = release_dir / "SUMMARY.zh-CN.md"
    summary_path.write_text(_release_summary_text(name, release_version, packaged), encoding="utf-8")

    manifest_path = release_dir / "release.json"
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return ReleaseResult(
        release_dir=release_dir,
        manifest_path=manifest_path,
        checksum_path=checksum_path,
        guide_path=guide_path,
        summary_path=summary_path,
        ai_guide_path=ai_guide_path,
        ai_manifest_path=ai_manifest_path,
        artifacts=packaged,
    )


def resolve_release_dir(project_root: str | Path, release_dir: str | Path | None = None) -> Path:
    """Resolve a release directory for verification."""
    if release_dir:
        candidate = Path(release_dir).expanduser().resolve()
        if candidate.exists():
            return candidate
        raise RuntimeError(f"没有找到发布目录：{candidate}")

    latest = latest_release_dir(project_root)
    if latest:
        return latest

    raise RuntimeError("当前没有找到可校验的发布目录。请先运行 `codepilot binary release`。")


def verify_release_bundle(release_dir: str | Path) -> VerificationResult:
    """Verify release metadata, checksums, and staged files."""
    root = Path(release_dir).expanduser().resolve()
    if not root.exists():
        raise RuntimeError(f"没有找到发布目录：{root}")

    manifest_path = root / "release.json"
    checksum_path = root / "SHA256SUMS.txt"
    issues: list[str] = []
    checked_files = 0

    if not manifest_path.exists():
        issues.append("缺少 release.json")
        return VerificationResult(root, manifest_path, checksum_path, checked_files, issues)
    if not checksum_path.exists():
        issues.append("缺少 SHA256SUMS.txt")
        return VerificationResult(root, manifest_path, checksum_path, checked_files, issues)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        issues.append(f"release.json 无法解析: {exc}")
        return VerificationResult(root, manifest_path, checksum_path, checked_files, issues)

    checksums: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw:
            continue
        if "  " not in raw:
            issues.append(f"SHA256SUMS.txt 存在无法识别的行: {raw}")
            continue
        digest, relative = raw.split("  ", 1)
        checksums[relative.replace("\\", "/")] = digest.strip()

    guide_path = root / "README.zh-CN.md"
    if not guide_path.exists():
        issues.append("缺少 README.zh-CN.md")
    ai_guide_path = root / "AI_USAGE.zh-CN.md"
    if not ai_guide_path.exists():
        issues.append("缺少 AI_USAGE.zh-CN.md")
    ai_manifest_path = root / "AI_MANIFEST.json"
    if not ai_manifest_path.exists():
        issues.append("缺少 AI_MANIFEST.json")
    else:
        try:
            json.loads(ai_manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(f"AI_MANIFEST.json 无法解析: {exc}")
    summary_path = root / "SUMMARY.zh-CN.md"
    if not summary_path.exists():
        issues.append("缺少 SUMMARY.zh-CN.md")

    for artifact in manifest.get("artifacts", []):
        staged = Path(artifact.get("staged_path", ""))
        archive = Path(artifact.get("archive_path", ""))
        install_script = Path(artifact.get("install_script", ""))
        for label, path in (("二进制", staged), ("压缩包", archive), ("安装脚本", install_script)):
            if not path.exists():
                issues.append(f"{label}不存在: {path}")

        if staged.exists():
            checked_files += 1
            actual = _sha256_file(staged)
            expected = artifact.get("binary_sha256", "")
            if actual != expected:
                issues.append(f"二进制校验不匹配: {staged.name}")
            rel = str(staged.relative_to(root)).replace("\\", "/")
            if checksums.get(rel) != actual:
                issues.append(f"SHA256SUMS.txt 中的二进制校验不匹配: {rel}")

        if archive.exists():
            checked_files += 1
            actual = _sha256_file(archive)
            expected = artifact.get("archive_sha256", "")
            if actual != expected:
                issues.append(f"压缩包校验不匹配: {archive.name}")
            rel = str(archive.relative_to(root)).replace("\\", "/")
            if checksums.get(rel) != actual:
                issues.append(f"SHA256SUMS.txt 中的压缩包校验不匹配: {rel}")
            archive_format = artifact.get("archive_format", "")
            if archive_format == "zip" and archive.suffix.lower() != ".zip":
                issues.append(f"压缩格式声明与文件后缀不一致: {archive.name}")
            if archive_format == "tar.gz" and not archive.name.lower().endswith(".tar.gz"):
                issues.append(f"压缩格式声明与文件后缀不一致: {archive.name}")
            try:
                members = _read_archive_members(archive, archive_format)
            except Exception as exc:
                issues.append(f"压缩包无法读取: {archive.name} ({exc})")
            else:
                folder_name = _archive_root_folder(archive, archive_format)
                expected_members = {
                    f"{folder_name}/{staged.name}",
                    f"{folder_name}/{install_script.name}",
                    f"{folder_name}/README.zh-CN.md",
                    f"{folder_name}/AI_USAGE.zh-CN.md",
                    f"{folder_name}/AI_MANIFEST.json",
                }
                missing = sorted(member for member in expected_members if member not in members)
                if missing:
                    issues.append(f"压缩包缺少预期文件: {archive.name} -> {', '.join(missing)}")

    return VerificationResult(root, manifest_path, checksum_path, checked_files, issues)


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
