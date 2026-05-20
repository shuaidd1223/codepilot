"""Release bundle creation and verification for CodePilot binaries."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from zipfile import ZIP_DEFLATED, ZipFile

from codepilot import __version__
from codepilot.ai_support.agent_support import ai_guide_markdown, manifest_json
from codepilot.binary_support.paths import (
    default_release_dir,
    infer_platform_tag,
    latest_built_binary,
    latest_release_dir,
    list_built_binaries,
)
from codepilot.binary_support.types import ReleaseArtifact, ReleaseResult, VerificationResult


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
    command_name = Path(binary_name).stem
    return (
        "@echo off\n"
        "setlocal\n"
        "set SCRIPT_DIR=%~dp0\n"
        "set TARGET_DIR=%LOCALAPPDATA%\\Programs\\CodePilot\\bin\n"
        "if not exist \"%TARGET_DIR%\" mkdir \"%TARGET_DIR%\"\n"
        f"copy /Y \"%SCRIPT_DIR%{binary_name}\" \"%TARGET_DIR%\\{binary_name}\" >nul\n"
        f"if exist \"%TARGET_DIR%\\{command_name}.cmd\" del /F /Q \"%TARGET_DIR%\\{command_name}.cmd\" >nul 2>nul\n"
        f"if exist \"%TARGET_DIR%\\{command_name}.bat\" del /F /Q \"%TARGET_DIR%\\{command_name}.bat\" >nul 2>nul\n"
        "powershell -NoProfile -Command \"$dir=$env:LOCALAPPDATA + '\\Programs\\CodePilot\\bin';"
        "$current=[Environment]::GetEnvironmentVariable('Path','User');"
        "if(-not $current){$current=''};"
        "$parts=@($current -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' });"
        "$dirKey=$dir.TrimEnd('\\').ToLowerInvariant();"
        "$filtered=@();"
        "$seen=@{};"
        "foreach($p in $parts){"
        "$key=$p.TrimEnd('\\').ToLowerInvariant();"
        "if($key -eq $dirKey){continue};"
        "if($seen.ContainsKey($key)){continue};"
        "$seen[$key]=$true;"
        "$filtered += $p;"
        "};"
        "$updatedParts=@($dir) + $filtered;"
        "$updated=($updatedParts -join ';');"
        "if($updated -ne $current){[Environment]::SetEnvironmentVariable('Path',$updated,'User')};"
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
    vendor_dir: Path | None = None,
    web_dir: Path | None = None,
    feishu_dir: Path | None = None,
) -> None:
    """Create the platform archive with binary, installer, and Chinese guide."""
    if archive_format == "zip":
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as bundle:
            bundle.write(staged_path, arcname=f"{folder_name}/{staged_path.name}")
            bundle.write(script_path, arcname=f"{folder_name}/{script_path.name}")
            bundle.write(guide_path, arcname=f"{folder_name}/README.zh-CN.md")
            bundle.write(ai_guide_path, arcname=f"{folder_name}/AI_USAGE.zh-CN.md")
            bundle.write(ai_manifest_path, arcname=f"{folder_name}/AI_MANIFEST.json")
            for vendor_file in _iter_vendor_files(vendor_dir):
                relative = str(vendor_file.relative_to(vendor_dir)).replace("\\", "/")
                bundle.write(vendor_file, arcname=f"{folder_name}/bin/vendor/{relative}")
            for web_file in _iter_tree_files(web_dir):
                relative = str(web_file.relative_to(web_dir)).replace("\\", "/")
                bundle.write(web_file, arcname=f"{folder_name}/web/{relative}")
            for feishu_file in _iter_tree_files(feishu_dir):
                relative = str(feishu_file.relative_to(feishu_dir)).replace("\\", "/")
                bundle.write(feishu_file, arcname=f"{folder_name}/feishu/{relative}")
        return

    if archive_format == "tar.gz":
        with tarfile.open(archive_path, "w:gz") as bundle:
            bundle.add(staged_path, arcname=f"{folder_name}/{staged_path.name}")
            bundle.add(script_path, arcname=f"{folder_name}/{script_path.name}")
            bundle.add(guide_path, arcname=f"{folder_name}/README.zh-CN.md")
            bundle.add(ai_guide_path, arcname=f"{folder_name}/AI_USAGE.zh-CN.md")
            bundle.add(ai_manifest_path, arcname=f"{folder_name}/AI_MANIFEST.json")
            for vendor_file in _iter_vendor_files(vendor_dir):
                relative = str(vendor_file.relative_to(vendor_dir)).replace("\\", "/")
                bundle.add(vendor_file, arcname=f"{folder_name}/bin/vendor/{relative}")
            for web_file in _iter_tree_files(web_dir):
                relative = str(web_file.relative_to(web_dir)).replace("\\", "/")
                bundle.add(web_file, arcname=f"{folder_name}/web/{relative}")
            for feishu_file in _iter_tree_files(feishu_dir):
                relative = str(feishu_file.relative_to(feishu_dir)).replace("\\", "/")
                bundle.add(feishu_file, arcname=f"{folder_name}/feishu/{relative}")
        return

    raise RuntimeError(f"不支持的压缩格式：{archive_format}")


def _iter_vendor_files(vendor_dir: Path | None) -> list[Path]:
    return _iter_tree_files(vendor_dir)


def _iter_tree_files(root: Path | None) -> list[Path]:
    if root is None or not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _stage_vendor_bundle(source_path: Path, artifact_dir: Path) -> Path | None:
    source_vendor_dir = source_path.parent / "bin" / "vendor"
    if not source_vendor_dir.exists():
        return None

    staged_vendor_dir = artifact_dir / "bin" / "vendor"
    if staged_vendor_dir.exists():
        shutil.rmtree(staged_vendor_dir)
    shutil.copytree(source_vendor_dir, staged_vendor_dir)
    return staged_vendor_dir


def _stage_web_assets(source_path: Path, artifact_dir: Path) -> Path | None:
    source_web_dir = source_path.parent / "web"
    if not source_web_dir.exists():
        return None

    staged_web_dir = artifact_dir / "web"
    if staged_web_dir.exists():
        shutil.rmtree(staged_web_dir)
    shutil.copytree(source_web_dir, staged_web_dir)
    return staged_web_dir


def _project_requires_web_assets(project_root: Path) -> bool:
    return (project_root / "codepilot" / "web" / "index.html").exists()


def _missing_web_assets(web_dir: Path) -> list[str]:
    required = [
        web_dir / "index.html",
        web_dir / "app.js",
        web_dir / "boundaries" / "AppStateBoundary.js",
    ]
    return [str(path.relative_to(web_dir)) for path in required if not path.exists()]


def _stage_feishu_runtime(source_path: Path, artifact_dir: Path) -> Path | None:
    source_feishu_dir = source_path.parent / "feishu"
    if not source_feishu_dir.exists():
        return None

    missing = _missing_feishu_runtime(source_feishu_dir)
    if missing:
        raise RuntimeError("飞书运行时目录不完整，不能生成不完整发布包：\n- " + "\n- ".join(missing))

    staged_feishu_dir = artifact_dir / "feishu"
    if staged_feishu_dir.exists():
        shutil.rmtree(staged_feishu_dir)
    shutil.copytree(source_feishu_dir, staged_feishu_dir)
    return staged_feishu_dir


def _project_requires_feishu_runtime(project_root: Path) -> bool:
    return (project_root / "codepilot" / "feishu_worker.mjs").exists()


def _missing_feishu_runtime(feishu_dir: Path) -> list[str]:
    required = [
        feishu_dir / "package.json",
        feishu_dir / "package-lock.json",
        feishu_dir / "node_modules" / "@larksuiteoapi" / "node-sdk",
        feishu_dir / "feishu_worker.mjs",
        feishu_dir / "feishu_notify.mjs",
    ]
    return [str(path.relative_to(feishu_dir)) for path in required if not path.exists()]


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
    ai_guide_path.write_text(ai_guide_markdown(command_name=name, language="zh-CN"), encoding="utf-8")
    ai_manifest_path = release_dir / "AI_MANIFEST.json"
    ai_manifest_path.write_text(
        manifest_json(version=release_version, command_name=name, binary_name=name, language="zh-CN"),
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
        vendor_dir = _stage_vendor_bundle(source_path, artifact_dir)
        web_dir = _stage_web_assets(source_path, artifact_dir)
        if web_dir is None and _project_requires_web_assets(project_root):
            raise RuntimeError("二进制产物缺少 Web UI 静态资源目录，不能生成不完整发布包。")
        if web_dir is not None:
            missing_web = _missing_web_assets(web_dir)
            if missing_web:
                raise RuntimeError("Web UI 静态资源目录不完整，不能生成不完整发布包：\n- " + "\n- ".join(missing_web))
        feishu_dir = _stage_feishu_runtime(source_path, artifact_dir)
        if feishu_dir is None and _project_requires_feishu_runtime(project_root):
            raise RuntimeError("二进制产物缺少飞书运行时目录，不能生成不完整发布包。")
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
            vendor_dir=vendor_dir,
            web_dir=web_dir,
            feishu_dir=feishu_dir,
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
                "web_assets": str(web_dir) if web_dir else "",
                "feishu_runtime": str(feishu_dir) if feishu_dir else "",
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


def _load_release_manifest(manifest_path: Path, issues: list[str]) -> dict | None:
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        issues.append(f"release.json 无法解析: {exc}")
        return None


def _read_release_checksums(checksum_path: Path, issues: list[str]) -> dict[str, str]:
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
    return checksums


def _verify_release_docs(root: Path, issues: list[str]) -> None:
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


def _verify_staged_file(
    root: Path,
    staged: Path,
    *,
    expected_digest: str,
    checksums: dict[str, str],
    issues: list[str],
) -> int:
    if not staged.exists():
        return 0

    actual = _sha256_file(staged)
    if actual != expected_digest:
        issues.append(f"二进制校验不匹配: {staged.name}")
    rel = str(staged.relative_to(root)).replace("\\", "/")
    if checksums.get(rel) != actual:
        issues.append(f"SHA256SUMS.txt 中的二进制校验不匹配: {rel}")
    return 1


def _verify_archive_members(
    archive: Path,
    *,
    archive_format: str,
    staged: Path,
    install_script: Path,
    web_dir: Path | None = None,
    feishu_dir: Path | None = None,
    issues: list[str],
) -> None:
    if archive_format == "zip" and archive.suffix.lower() != ".zip":
        issues.append(f"压缩格式声明与文件后缀不一致: {archive.name}")
    if archive_format == "tar.gz" and not archive.name.lower().endswith(".tar.gz"):
        issues.append(f"压缩格式声明与文件后缀不一致: {archive.name}")

    try:
        members = _read_archive_members(archive, archive_format)
    except Exception as exc:
        issues.append(f"压缩包无法读取: {archive.name} ({exc})")
        return

    folder_name = _archive_root_folder(archive, archive_format)
    expected_members = {
        f"{folder_name}/{staged.name}",
        f"{folder_name}/{install_script.name}",
        f"{folder_name}/README.zh-CN.md",
        f"{folder_name}/AI_USAGE.zh-CN.md",
        f"{folder_name}/AI_MANIFEST.json",
    }
    for web_file in _iter_tree_files(web_dir):
        relative = str(web_file.relative_to(web_dir)).replace("\\", "/")
        expected_members.add(f"{folder_name}/web/{relative}")
    for feishu_file in _iter_tree_files(feishu_dir):
        relative = str(feishu_file.relative_to(feishu_dir)).replace("\\", "/")
        expected_members.add(f"{folder_name}/feishu/{relative}")
    missing = sorted(member for member in expected_members if member not in members)
    if missing:
        issues.append(f"压缩包缺少预期文件: {archive.name} -> {', '.join(missing)}")


def _verify_archive_file(
    root: Path,
    archive: Path,
    *,
    expected_digest: str,
    checksums: dict[str, str],
    archive_format: str,
    staged: Path,
    install_script: Path,
    web_dir: Path | None,
    feishu_dir: Path | None,
    issues: list[str],
) -> int:
    if not archive.exists():
        return 0

    actual = _sha256_file(archive)
    if actual != expected_digest:
        issues.append(f"压缩包校验不匹配: {archive.name}")
    rel = str(archive.relative_to(root)).replace("\\", "/")
    if checksums.get(rel) != actual:
        issues.append(f"SHA256SUMS.txt 中的压缩包校验不匹配: {rel}")

    _verify_archive_members(
        archive,
        archive_format=archive_format,
        staged=staged,
        install_script=install_script,
        web_dir=web_dir,
        feishu_dir=feishu_dir,
        issues=issues,
    )
    return 1


def _verify_manifest_artifact(
    root: Path,
    artifact: dict,
    *,
    checksums: dict[str, str],
    issues: list[str],
) -> int:
    staged = Path(artifact.get("staged_path", ""))
    archive = Path(artifact.get("archive_path", ""))
    install_script = Path(artifact.get("install_script", ""))
    web_dir = Path(artifact.get("web_assets", "")) if artifact.get("web_assets") else None
    feishu_dir = Path(artifact.get("feishu_runtime", "")) if artifact.get("feishu_runtime") else None
    for label, path in (("二进制", staged), ("压缩包", archive), ("安装脚本", install_script)):
        if not path.exists():
            issues.append(f"{label}不存在: {path}")
    if web_dir is not None and not web_dir.exists():
        issues.append(f"Web 静态资源目录不存在: {web_dir}")
    if feishu_dir is not None and not feishu_dir.exists():
        issues.append(f"飞书运行时目录不存在: {feishu_dir}")

    checked_files = 0
    checked_files += _verify_staged_file(
        root,
        staged,
        expected_digest=artifact.get("binary_sha256", ""),
        checksums=checksums,
        issues=issues,
    )
    checked_files += _verify_archive_file(
        root,
        archive,
        expected_digest=artifact.get("archive_sha256", ""),
        checksums=checksums,
        archive_format=artifact.get("archive_format", ""),
        staged=staged,
        install_script=install_script,
        web_dir=web_dir,
        feishu_dir=feishu_dir,
        issues=issues,
    )
    return checked_files


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

    manifest = _load_release_manifest(manifest_path, issues)
    if manifest is None:
        return VerificationResult(root, manifest_path, checksum_path, checked_files, issues)

    checksums = _read_release_checksums(checksum_path, issues)
    _verify_release_docs(root, issues)

    for artifact in manifest.get("artifacts", []):
        checked_files += _verify_manifest_artifact(
            root,
            artifact,
            checksums=checksums,
            issues=issues,
        )

    return VerificationResult(root, manifest_path, checksum_path, checked_files, issues)
