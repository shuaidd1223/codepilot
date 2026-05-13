"""Dependency health signal collector helpers."""

from __future__ import annotations

import json
from pathlib import Path

from codepilot.commands.inspect_signal_collectors_shared import should_skip_scan_path


def _manifest_lock_status(manifest: Path, locks: tuple[str, ...]) -> str:
    existing = [manifest.parent / lock for lock in locks if (manifest.parent / lock).exists()]
    if not existing:
        return "missing"
    newest_lock = max(existing, key=lambda item: item.stat().st_mtime)
    return "stale" if manifest.stat().st_mtime > newest_lock.stat().st_mtime else "ok"


def _count_unpinned_requirements(path: Path) -> int:
    count = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(("-", "--")):
            continue
        if not any(op in line for op in ("==", ">=", "<=", "~=", ">", "<")):
            count += 1
    return count


def collect_dependency_health(project_path: Path, limit: int = 20) -> str:
    """Collect cheap, offline dependency health signals without hitting registries."""
    findings: list[str] = []
    package_manifests = [
        path
        for path in project_path.rglob("package.json")
        if path.is_file() and not should_skip_scan_path(path)
    ]
    for package_json in package_manifests:
        if len(findings) >= limit:
            break
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        has_dependencies = bool(
            data.get("dependencies")
            or data.get("devDependencies")
            or data.get("optionalDependencies")
        )
        if not has_dependencies:
            continue
        rel = package_json.relative_to(project_path)
        status = _manifest_lock_status(package_json, ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"))
        if status == "missing":
            findings.append(f"{rel}: 发现依赖但缺少 lockfile")
        elif status == "stale":
            findings.append(f"{rel}: package.json 比 lockfile 更新，可能需要刷新依赖锁")

    for req in project_path.rglob("requirements*.txt"):
        if len(findings) >= limit:
            break
        if not req.is_file() or should_skip_scan_path(req):
            continue
        unpinned = _count_unpinned_requirements(req)
        if unpinned:
            rel = req.relative_to(project_path)
            findings.append(f"{rel}: {unpinned} 个依赖未固定版本")

    return "\n".join(findings[:limit]) if findings else "（无明显依赖健康问题；未联网检查最新版本）"
