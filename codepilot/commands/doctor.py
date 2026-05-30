"""codepilot doctor — environment self-check command."""

from __future__ import annotations
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.output import echo, safe
from codepilot.storage import database as db


# ── Check result model ───────────────────────────────────────────────────────

class CheckResult:
    """Single diagnostic check result."""

    __slots__ = ("name", "ok", "severity", "detail", "fix", "extra")

    def __init__(
        self,
        name: str,
        ok: bool,
        detail: str,
        fix: Optional[str] = None,
        *,
        severity: Optional[str] = None,
        extra: Optional[dict] = None,
    ):
        resolved_severity = severity or ("ok" if ok else "error")
        if resolved_severity not in {"ok", "warning", "error", "info"}:
            raise ValueError(f"unsupported severity: {resolved_severity}")
        self.name = name
        self.ok = resolved_severity != "error"
        self.severity = resolved_severity
        self.detail = detail
        self.fix = fix  # suggested remediation command / hint
        self.extra = dict(extra or {})

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
        }
        if self.fix:
            d["fix"] = self.fix
        for key, value in self.extra.items():
            if key not in d:
                d[key] = value
        return d


def _summary_status_emoji(results: list["CheckResult"]) -> str:
    """Return a compact emoji for the aggregated doctor result."""
    if any(item.severity == "error" for item in results):
        return "✘"
    return "✓"


# ── Individual checks ────────────────────────────────────────────────────────

def _check_python_version() -> CheckResult:
    """Python >= 3.9."""
    vi = sys.version_info
    ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    if (vi.major, vi.minor) >= (3, 9):
        return CheckResult("python_version", True, f"Python {ver}")
    return CheckResult(
        "python_version", False, f"Python {ver} (需要 >= 3.9)",
        fix="请安装 Python 3.9 或更高版本: https://www.python.org/downloads/",
    )


def _check_git(project_root: Optional[Path]) -> list[CheckResult]:
    """Git init, clean workspace, base_branch existence."""
    results: list[CheckResult] = []
    cwd = str(project_root) if project_root else None

    # 1) git init?
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=cwd, check=True,
        )
        results.append(CheckResult("git_init", True, "Git 仓库已初始化"))
    except (subprocess.CalledProcessError, FileNotFoundError):
        results.append(CheckResult(
            "git_init", False, "当前目录不是 Git 仓库",
            fix="git init",
        ))
        return results  # further checks meaningless

    # 2) clean workspace?
    cp = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=cwd,
    )
    dirty = cp.stdout.strip()
    if not dirty:
        results.append(CheckResult("git_clean", True, "工作区干净"))
    else:
        n_changes = len(dirty.splitlines())
        results.append(CheckResult(
            "git_clean", False, f"工作区有 {n_changes} 个未提交的变更",
            fix="git add -A && git commit -m 'wip'",
        ))

    # 3) base_branch exists?
    from codepilot.core.config import load_config
    cfg = load_config()
    base = cfg.base_branch if cfg else "dev"
    cp2 = subprocess.run(
        ["git", "rev-parse", "--verify", base],
        capture_output=True, text=True, cwd=cwd,
    )
    if cp2.returncode == 0:
        results.append(CheckResult("base_branch", True, f"base_branch '{base}' 存在"))
    else:
        results.append(CheckResult(
            "base_branch", False, f"base_branch '{base}' 不存在",
            fix=f"git branch {base}  # 或在 AGENTS.toml 中修改 base_branch",
        ))

    return results


def _check_agents_toml() -> CheckResult:
    """AGENTS.toml parseable & key fields present."""
    from codepilot.core.config import find_config

    config_path = find_config()
    if config_path is None:
        return CheckResult(
            "agents_toml", False, "未找到 AGENTS.toml",
            fix="codepilot init",
        )

    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib

        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        return CheckResult(
            "agents_toml", False, f"AGENTS.toml 解析失败: {safe(str(exc))}",
            fix=f"检查 {config_path} 的 TOML 语法",
        )

    required_sections = ("project", "agents", "automation")
    missing = [s for s in required_sections if s not in data]
    if missing:
        return CheckResult(
            "agents_toml", False,
            f"AGENTS.toml 缺少必要段: {', '.join(missing)}",
            fix=f"在 {config_path} 中补充 [{'], ['.join(missing)}] 段",
        )

    project = data.get("project", {})
    required_keys = ("name", "base_branch")
    missing_keys = [k for k in required_keys if k not in project]
    if missing_keys:
        return CheckResult(
            "agents_toml", False,
            f"[project] 缺少关键字段: {', '.join(missing_keys)}",
            fix=f"在 {config_path} 的 [project] 段中补充 {', '.join(missing_keys)}",
        )

    return CheckResult("agents_toml", True, f"AGENTS.toml 可解析 ({config_path})")


def _check_cli_tools() -> list[CheckResult]:
    """codex / claude / opencode CLI on PATH."""
    from codepilot.ai_support.providers import resolve_cli_provider

    results: list[CheckResult] = []
    install_hints = {
        "codex": "npm install -g @openai/codex",
        "claude": "codepilot setup --install-claude",
        # OpenCode is the bottom-tier fallback; absence is a warning, not an error.
        "opencode": "npm install -g opencode-ai (or visit https://opencode.ai)",
    }
    for tool in ("codex", "claude", "opencode"):
        try:
            provider = resolve_cli_provider(tool)
            found_path = provider.find_executable()
        except Exception:
            found = shutil.which(tool)
            found_path = Path(found) if found else None
        if found_path:
            results.append(CheckResult(f"cli_{tool}", True, f"{tool} -> {found_path}"))
        else:
            results.append(CheckResult(
                f"cli_{tool}", False, f"'{tool}' 不在 PATH 中",
                fix=install_hints.get(tool, f"install {tool}"),
            ))
    return results


BundledVersionRunner = Callable[[Path, list[str]], str]
_BUNDLED_VERSION_RE = re.compile(r"\b\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?\b")
_WINDOWS_EXECUTABLE_SUFFIXES = {".exe", ".cmd", ".bat", ".ps1"}


def _bundled_cli_manifest_candidates(install_dir: str | Path | None = None) -> list[Path]:
    """Return possible installed/bundled vendor manifest locations."""
    from codepilot.binary_support.paths import default_install_dir, running_binary_path
    from codepilot.binary_support.vendor_fetcher import BUNDLED_VENDOR_MANIFEST

    candidates: list[Path] = []
    if install_dir:
        root = Path(install_dir).expanduser().resolve()
        candidates.append(root / "vendor" / BUNDLED_VENDOR_MANIFEST)
        candidates.append(root / "bin" / "vendor" / BUNDLED_VENDOR_MANIFEST)

    running_binary = running_binary_path()
    if running_binary:
        binary_dir = running_binary.parent
        candidates.append(binary_dir / "vendor" / BUNDLED_VENDOR_MANIFEST)
        candidates.append(binary_dir / "bin" / "vendor" / BUNDLED_VENDOR_MANIFEST)
        candidates.append(binary_dir.parent / "vendor" / BUNDLED_VENDOR_MANIFEST)

    try:
        default_root = default_install_dir()
        candidates.append(default_root / "vendor" / BUNDLED_VENDOR_MANIFEST)
    except Exception:
        pass

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.normpath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _read_bundled_cli_manifest(install_dir: str | Path | None = None) -> tuple[Path | None, dict | None, str]:
    for candidate in _bundled_cli_manifest_candidates(install_dir):
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            return candidate, None, f"vendor manifest 无法解析: {safe(str(exc))}"
        if not isinstance(payload, dict):
            return candidate, None, "vendor manifest 顶层结构不是对象"
        return candidate, payload, ""
    return None, None, ""


def _manifest_root(manifest_path: Path) -> Path:
    if manifest_path.parent.name.lower() == "vendor" and manifest_path.parent.parent.name.lower() == "bin":
        return manifest_path.parent.parent.parent
    if manifest_path.parent.name.lower() == "vendor":
        return manifest_path.parent.parent
    return manifest_path.parent


def _resolve_vendor_entry_path(root: Path, raw_path: str, provider: str) -> Path:
    relative = Path(str(raw_path or "").replace("\\", "/"))
    if not relative.name:
        relative = Path("vendor") / provider
    candidates = [(root / relative).resolve()]
    if len(relative.parts) > 1 and relative.parts[0].lower() == "bin":
        candidates.append((root / Path(*relative.parts[1:])).resolve())
    if relative.parts and relative.parts[0].lower() != "vendor":
        candidates.append((root / "vendor" / relative.name).resolve())

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _is_bundled_cli_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if platform.system().lower() == "windows":
        return path.suffix.lower() in _WINDOWS_EXECUTABLE_SUFFIXES or path.suffix == ""
    return bool(path.stat().st_mode & 0o111) and os.access(path, os.X_OK)


def _default_bundled_version_runner(command: Path, args: list[str]) -> str:
    from codepilot.core.text_decode import decode_subprocess_text

    result = subprocess.run(
        [str(command), *args],
        capture_output=True,
        text=False,
        timeout=20,
    )
    stdout = decode_subprocess_text(result.stdout).strip()
    stderr = decode_subprocess_text(result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(stderr or stdout or f"exit code {result.returncode}")
    return stdout or stderr


def _base_semver(value: str) -> str:
    match = re.search(r"\d+(?:\.\d+){1,3}", value or "")
    return match.group(0) if match else value.strip()


def _observed_version(output: str) -> str:
    match = _BUNDLED_VERSION_RE.search(output or "")
    return match.group(0) if match else ""


def _version_matches_manifest(expected: str, output: str) -> tuple[bool, str]:
    expected_version = str(expected or "").strip()
    observed = _observed_version(output)
    if not expected_version:
        return False, observed
    if expected_version in (output or ""):
        return True, observed or expected_version
    if observed and observed == expected_version:
        return True, observed
    if observed and _base_semver(observed) == _base_semver(expected_version):
        return True, observed
    return False, observed


def _bundled_cli_result(
    provider: str,
    ok: bool,
    detail: str,
    *,
    status: str,
    severity: str | None = None,
    fix: str | None = None,
    manifest_path: Path | None = None,
    executable_path: Path | None = None,
    expected_version: str = "",
    observed_version: str = "",
) -> CheckResult:
    extra = {
        "provider": provider,
        "kind": "bundled_cli",
        "status": status,
    }
    if manifest_path:
        extra["manifest_path"] = str(manifest_path)
    if executable_path:
        extra["path"] = str(executable_path)
    if expected_version:
        extra["expected_version"] = expected_version
    if observed_version:
        extra["observed_version"] = observed_version
    return CheckResult(
        f"bundled_cli_{provider}",
        ok,
        detail,
        fix=fix,
        severity=severity,
        extra=extra,
    )


def _check_bundled_cli_tools(
    *,
    install_dir: str | Path | None = None,
    version_runner: BundledVersionRunner | None = None,
) -> list[CheckResult]:
    """Check installed bundled opencode/codex vendor binaries against their manifest."""
    from codepilot.binary_support.vendor_fetcher import SUPPORTED_BUNDLED_CLI_PROVIDERS

    manifest_path, manifest, manifest_error = _read_bundled_cli_manifest(install_dir)
    if manifest_error:
        return [
            CheckResult(
                "bundled_cli_manifest",
                False,
                manifest_error,
                fix="重新运行 codepilot binary install 或 codepilot self-update --providers all。",
                severity="error",
                extra={"kind": "bundled_cli", "status": "invalid_manifest", "manifest_path": str(manifest_path)},
            )
        ]
    if manifest_path is None or manifest is None:
        return [
            CheckResult(
                "bundled_cli_manifest",
                True,
                "未找到 bundled CLI vendor manifest；可能是源码运行或旧版安装，跳过 bundled CLI 检查。",
                fix="如需 bundled CLI，可运行 codepilot binary build --bundle-cli=opencode,codex 后重新安装。",
                severity="warning",
                extra={"kind": "bundled_cli", "status": "manifest_missing"},
            )
        ]

    raw_providers = manifest.get("providers")
    if not isinstance(raw_providers, list):
        return [
            CheckResult(
                "bundled_cli_manifest",
                False,
                f"vendor manifest 缺少 providers 列表 ({manifest_path})",
                fix="重新运行 codepilot binary install 或 codepilot self-update --providers all。",
                severity="error",
                extra={"kind": "bundled_cli", "status": "invalid_manifest", "manifest_path": str(manifest_path)},
            )
        ]

    runner = version_runner or _default_bundled_version_runner
    root = _manifest_root(manifest_path)
    entries: dict[str, dict] = {}
    for item in raw_providers:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip().lower()
        if provider in SUPPORTED_BUNDLED_CLI_PROVIDERS:
            entries[provider] = item

    results: list[CheckResult] = []
    for provider in SUPPORTED_BUNDLED_CLI_PROVIDERS:
        entry = entries.get(provider)
        if not entry:
            results.append(
                _bundled_cli_result(
                    provider,
                    True,
                    f"{provider} 未包含在 bundled CLI manifest 中",
                    status="not_bundled",
                    severity="warning",
                    manifest_path=manifest_path,
                )
            )
            continue

        expected_version = str(entry.get("version") or "").strip()
        executable_path = _resolve_vendor_entry_path(root, str(entry.get("path") or ""), provider)
        if not executable_path.exists():
            results.append(
                _bundled_cli_result(
                    provider,
                    False,
                    f"{provider} bundled vendor 文件不存在: {executable_path}",
                    status="missing",
                    fix="重新运行 codepilot binary install 或 codepilot self-update --providers all。",
                    manifest_path=manifest_path,
                    executable_path=executable_path,
                    expected_version=expected_version,
                )
            )
            continue
        if not _is_bundled_cli_executable(executable_path):
            results.append(
                _bundled_cli_result(
                    provider,
                    False,
                    f"{provider} bundled vendor 文件不可执行: {executable_path}",
                    status="not_executable",
                    fix=f"检查文件权限，或重新运行 codepilot self-update --providers {provider}。",
                    manifest_path=manifest_path,
                    executable_path=executable_path,
                    expected_version=expected_version,
                )
            )
            continue

        try:
            version_output = runner(executable_path, ["--version"])
        except Exception as exc:
            results.append(
                _bundled_cli_result(
                    provider,
                    False,
                    f"{provider} --version 执行失败: {safe(str(exc))}",
                    status="version_failed",
                    fix=f"重新运行 codepilot self-update --providers {provider}。",
                    manifest_path=manifest_path,
                    executable_path=executable_path,
                    expected_version=expected_version,
                )
            )
            continue

        version_ok, observed = _version_matches_manifest(expected_version, str(version_output or ""))
        if not version_ok:
            results.append(
                _bundled_cli_result(
                    provider,
                    False,
                    f"{provider} bundled 版本不匹配: manifest={expected_version or '-'} actual={observed or safe(str(version_output or '')[:120]) or '-'}",
                    status="version_mismatch",
                    fix=f"重新运行 codepilot self-update --providers {provider}。",
                    manifest_path=manifest_path,
                    executable_path=executable_path,
                    expected_version=expected_version,
                    observed_version=observed,
                )
            )
            continue

        results.append(
            _bundled_cli_result(
                provider,
                True,
                f"{provider} bundled CLI 正常 ({executable_path}, version {observed or expected_version})",
                status="healthy",
                manifest_path=manifest_path,
                executable_path=executable_path,
                expected_version=expected_version,
                observed_version=observed or expected_version,
            )
        )

    return results


def _check_api_keys() -> list[CheckResult]:
    """Check API key availability for commonly used providers."""
    from codepilot.ai_support.service import API_PROVIDERS
    from codepilot.ai_support.providers import resolve_api_provider
    from codepilot.core.config import load_config

    results: list[CheckResult] = []
    cfg = load_config()
    key_groups: dict[str, dict[str, object]] = {}
    required_provider = ""

    for key, base_provider in API_PROVIDERS.items():
        provider = resolve_api_provider(key, cfg.config_file_path if cfg else None)

        if not provider.requires_api_key():
            group = key_groups.setdefault("__local__", {"local": []})
            group["local"].append(key)
            continue

        env_var = provider.api_env_vars[0] if provider.api_env_vars else key
        group = key_groups.setdefault(env_var, {
            "env_var": env_var,
            "resolved": [],
            "missing": [],
        })
        source = ""
        if provider.api_key:
            source = "配置文件"
        else:
            for candidate in provider.api_env_vars:
                if os.environ.get(candidate, "").strip():
                    source = f"环境变量 {candidate}"
                    break

        if provider.resolve_api_key():
            group["resolved"].append((key, source))
        else:
            group["missing"].append(key)

    for group_key, group in key_groups.items():
        if group_key == "__local__":
            local_names = group["local"]
            if local_names:
                results.append(CheckResult(
                    "api_key_local",
                    True,
                    f"本地 provider 无需 API Key（可用: {', '.join(local_names[:3])}）",
                ))
            continue

        env_var = str(group["env_var"])
        resolved = list(group["resolved"])
        missing = list(group["missing"])
        label = f"api_key_{env_var.lower()}"
        is_required = required_provider in missing
        source_items = sorted({source for _, source in resolved if source})

        if not missing:
            resolved_names = [name for name, _ in resolved]
            detail = f"{env_var} 已配置（可用: {', '.join(resolved_names[:3])}"
            if source_items:
                detail += f"；来源: {', '.join(source_items)}"
            detail += "）"
            results.append(CheckResult(label, True, detail, severity="ok"))
            continue

        missing_str = ", ".join(missing[:3])
        resolved_names = [name for name, _ in resolved]
        if is_required:
            if resolved_names:
                detail = (
                    f"{env_var} 仅部分配置（已配置: {', '.join(resolved_names[:3])}"
                    f"；未配置: {missing_str}；当前配置必需: {required_provider}）"
                )
            else:
                detail = (
                    f"{env_var} 未设置（影响: {missing_str}；当前配置必需: "
                    f"{required_provider}）"
                )
            results.append(CheckResult(
                label,
                False,
                detail,
                fix=f"设置环境变量 {env_var}，或在 AGENTS.toml [providers] 中配置 api_key",
                severity="error",
            ))
            continue

        # 检查配置文件中是否有显式启用该 provider 且配置了 api_key
        # 只有当用户主动启用了某个 provider 但没有配置 api_key 时才提示
        has_explicit_enabled_provider = False
        if cfg and cfg.config_file_path:
            try:
                if sys.version_info >= (3, 11):
                    import tomllib
                else:
                    import tomli as tomllib
                with open(cfg.config_file_path, "rb") as f:
                    config_data = tomllib.load(f)
                providers_section = config_data.get("providers", {})
                for provider_name in missing:
                    provider_config = providers_section.get(provider_name, {})
                    if provider_config.get("enabled", False) and not provider_config.get("api_key", "").strip():
                        has_explicit_enabled_provider = True
                        break
            except Exception:
                pass

        # 只有显式启用但未配置 api_key 的才显示提示，否则静默跳过
        if not has_explicit_enabled_provider:
            continue

        if resolved_names:
            detail = (
                f"{env_var} 仅部分配置（已配置: {', '.join(resolved_names[:3])}"
                f"；其余可选未配置: {missing_str}"
            )
            if source_items:
                detail += f"；来源: {', '.join(source_items)}"
            detail += "）"
        else:
            detail = f"{env_var} 未设置（影响: {missing_str}；当前为可选）"
        results.append(CheckResult(
            label,
            True,
            detail,
            fix=f"设置环境变量 {env_var}，或在 AGENTS.toml [providers] 中配置 api_key",
            severity="info",
        ))

    return results


def _check_db_path() -> CheckResult:
    """Task DB path readable & writable."""
    from codepilot.storage.config import get_db_path

    db_path = get_db_path()
    if db_path.exists():
        # test read + write
        readable = os.access(db_path, os.R_OK)
        writable = os.access(db_path, os.W_OK)
        if readable and writable:
            return CheckResult("task_db", True, f"任务数据库可读写 ({db_path})")
        perms = []
        if not readable:
            perms.append("不可读")
        if not writable:
            perms.append("不可写")
        return CheckResult(
            "task_db", False,
            f"任务数据库 {', '.join(perms)} ({db_path})",
            fix=f"检查文件权限: {db_path}",
        )
    else:
        # db doesn't exist yet — check parent writable
        parent = db_path.parent
        if parent.exists() and os.access(parent, os.W_OK):
            return CheckResult("task_db", True, f"任务数据库目录可写 ({parent})")
        return CheckResult(
            "task_db", False,
            f"任务数据库目录不可写 ({parent})",
            fix=f"mkdir -p {parent}",
        )


def _check_preflight_dirty_worktree(project_info: dict) -> CheckResult | None:
    """Warn when ``preflight_dirty_worktree = "stop"`` and the workspace is dirty."""
    from codepilot.core.config import load_project_config, normalize_preflight_dirty_worktree

    cfg = load_project_config(project_info)
    if not cfg:
        return None
    policy = normalize_preflight_dirty_worktree(
        str(getattr(getattr(cfg, "automation", None), "preflight_dirty_worktree", "stop") or "stop")
    )
    if policy != "stop":
        return None

    project_path = Path(project_info["path"])
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=str(project_path), check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    cp = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(project_path),
    )
    dirty = cp.stdout.strip()
    if not dirty:
        return None

    n_changes = len(dirty.splitlines())
    task_workspace = str(
        getattr(getattr(cfg, "automation", None), "task_workspace", "branch") or "branch"
    ).strip().lower()

    detail = (
        f"工作区有 {n_changes} 个未提交变更，"
        f"且 [automation] 中 preflight_dirty_worktree = \"stop\""
    )
    fix = (
        "选择以下一种方式解除阻塞:\n"
        "  1. 提交改动: git add -A && git commit -m 'wip'\n"
        "  2. 暂存改动: git stash push --include-untracked -m '工作区暂存'\n"
        "  3. 允许自动提交: 在 AGENTS.toml [automation] 中设置 preflight_dirty_worktree = 'commit'\n"
        "  4. 允许自动暂存: 在 AGENTS.toml [automation] 中设置 preflight_dirty_worktree = 'stash'\n"
        "  5. 使用独立 worktree: 在 AGENTS.toml [automation] 中设置 task_workspace = 'branch' 或 'worktree'"
    )
    return CheckResult(
        "preflight_dirty_worktree",
        True,
        detail,
        fix=fix,
        severity="warning",
        extra={
            "dirty_changes": n_changes,
            "policy": "stop",
            "task_workspace": task_workspace,
        },
    )


def _check_console_encoding() -> CheckResult:
    """Console encoding is UTF-8."""
    stdout_enc = getattr(sys.stdout, "encoding", None) or ""
    if stdout_enc.lower().replace("-", "").startswith("utf"):
        return CheckResult("console_encoding", True, f"控制台编码: {stdout_enc}")
    return CheckResult(
        "console_encoding", False,
        f"控制台编码不是 UTF-8 (当前: {stdout_enc})",
        fix="设置环境变量 PYTHONUTF8=1 或 PYTHONIOENCODING=utf-8",
    )


# ── Run all checks ───────────────────────────────────────────────────────────

def run_all_checks() -> list[CheckResult]:
    """Execute every diagnostic check and return results."""
    results: list[CheckResult] = []
    results.append(_check_python_version())
    results.extend(_check_git(None))
    results.append(_check_agents_toml())
    results.extend(_check_cli_tools())
    results.extend(_check_bundled_cli_tools())
    results.extend(_check_api_keys())
    results.append(_check_db_path())
    results.append(_check_console_encoding())
    return results


def _resolve_project(project: str | None) -> dict | None:
    db.init_db()
    if project:
        found = db.get_project(project)
        if not found:
            raise click.ClickException(f"项目 '{project}' 未注册。")
        return found
    found = db.find_project_by_path(Path.cwd())
    if found:
        return found
    projects = db.list_projects()
    if len(projects) == 1:
        return projects[0]
    return None


def _project_config_check(project_info: dict) -> CheckResult:
    from codepilot.core.config import load_project_config

    cfg = load_project_config(project_info)
    config_file = str(project_info.get("config_file") or "")
    if not cfg:
        return CheckResult(
            "project_config",
            False,
            f"项目配置不可读取 ({config_file or project_info.get('path')})",
            fix="检查 AGENTS.toml 是否存在且语法正确。",
        )
    return CheckResult("project_config", True, f"项目配置可读取 ({config_file or cfg.config_file_path or project_info.get('path')})")


def _service_check(name: str, status: dict, state: dict | None = None) -> CheckResult:
    running = bool(status.get("running"))
    pid = int(status.get("pid") or 0)
    log_path = str(status.get("log") or (state or {}).get("log_path") or "")
    if running:
        detail = f"运行中 pid={pid}"
        if log_path:
            detail += f" log={log_path}"
        return CheckResult(f"service_{name}", True, detail)

    raw_pid = int((state or {}).get("pid") or 0)
    if state and raw_pid:
        detail = f"stale meta: recorded pid={raw_pid} status={(state or {}).get('status') or '-'}"
        if log_path:
            detail += f" log={log_path}"
        return CheckResult(
            f"service_{name}",
            True,
            detail,
            fix=f"服务 {name} 记录了已退出进程；可按需运行对应 status/stop 命令清理。",
            severity="warning",
        )
    detail = "未运行"
    if log_path:
        detail += f" log={log_path}"
    return CheckResult(f"service_{name}", True, detail)


def _webui_status_from_state() -> dict:
    from codepilot.core.runtime import is_process_alive
    from codepilot.commands import webui_service

    state = db.get_service_state("webui", "_global")
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    try:
        pid = int((state or {}).get("pid") or 0)
    except Exception:
        pid = 0
    running = bool(pid and is_process_alive(pid))
    return {
        "running": running,
        "pid": pid if running else 0,
        "log": str((state or {}).get("log_path") or webui_service.LOG_FILE),
        "host": meta.get("host") or "",
        "port": meta.get("port") or "",
    }


def _feishu_config_check(project_info: dict | None) -> CheckResult:
    from codepilot.core.config import load_project_config

    cfg = load_project_config(project_info) if project_info else load_project_config()
    if not cfg or not cfg.feishu_bot_enabled:
        return CheckResult("feishu_config", True, "飞书服务未启用")
    if not str(cfg.feishu_app_secret or "").strip():
        return CheckResult(
            "feishu_config",
            True,
            "飞书服务已启用，但缺少 feishu_bot.app_secret。",
            fix="将 [feishu_bot].app_secret 写入 .codepilot.secrets.toml，不要写入 AGENTS.toml。",
            severity="warning",
        )
    return CheckResult("feishu_config", True, "飞书服务已启用，secret 已通过配置解析。")


def run_project_checks(project_info: dict | None, *, include_services: bool = False) -> list[CheckResult]:
    results: list[CheckResult] = []
    if project_info:
        results.append(_project_config_check(project_info))
        preflight_check = _check_preflight_dirty_worktree(project_info)
        if preflight_check:
            results.append(preflight_check)
    results.append(_feishu_config_check(project_info))
    if not include_services:
        return results

    project_name = str(project_info.get("name") or "") if project_info else ""
    try:
        from codepilot.commands.daemon import daemon_service_status

        daemon_status = daemon_service_status(project_name or None)
    except Exception as exc:
        daemon_status = {"running": False, "log": "", "error": str(exc)}
    results.append(_service_check("daemon", daemon_status, db.get_service_state("daemon", project_name)))

    if project_name:
        try:
            from codepilot.commands.inspect import inspect_service_status

            inspect_status = inspect_service_status(project_name)
        except Exception as exc:
            inspect_status = {"running": False, "log": "", "error": str(exc)}
        results.append(_service_check("inspect", inspect_status, db.get_service_state("inspect", project_name)))
    else:
        results.append(CheckResult("service_inspect", True, "未指定项目，跳过 inspect 项目服务检查", severity="warning"))

    results.append(_service_check("webui", _webui_status_from_state(), db.get_service_state("webui", "_global")))

    try:
        from codepilot.commands.feishu import _service_status as feishu_service_status

        feishu_status = feishu_service_status()
    except Exception as exc:
        feishu_status = {"running": False, "log": "", "error": str(exc)}
    results.append(_service_check("feishu", feishu_status, db.get_service_state("feishu", "_global")))
    return results


def _run_setup_fix(project_name: str | None) -> dict:
    """Run the full automatic repair used by ``doctor --fix``.

    Fixes everything that can be safely auto-repaired:
    * global AGENTS.toml + .codepilot.secrets.toml
    * project-level AGENTS.toml + .codepilot.secrets.toml + .gitignore
    * SQLite database initialization
    * console encoding on Windows

    Returns a dict of ``{action: status, ...}`` suitable for JSON output.
    """
    import os
    from pathlib import Path

    from codepilot.binary_support.manager import ensure_global_config
    from codepilot.commands.config_cmd import (
        _canonical_config,
        ensure_secrets_template,
        render_agents_toml,
    )
    from codepilot.core.config import find_config, resolve_global_config_path
    from codepilot.core.gitignore import ensure_gitignore_entry
    from codepilot.core.console_encoding import configure_console_encoding
    from codepilot.storage.database import init_db as _init_db

    actions: dict[str, str] = {}

    # 1. Global config + secrets
    global_config = ensure_global_config()
    if global_config:
        actions["global_config"] = "created"
        actions["global_secrets"] = "created"
    else:
        # Global config exists — ensure secrets template is there too
        secrets_path = resolve_global_config_path().parent
        created = ensure_secrets_template(secrets_path)
        actions["global_config"] = "exists"
        actions["global_secrets"] = "created" if created else "exists"

    # 2. Project-level config (if we're in a project directory)
    project_config = find_config(Path.cwd())
    if project_config is None:
        # Try to create one in the current directory if it looks like a project
        cwd = Path.cwd()
        if (cwd / ".git").exists():
            config_path = cwd / "AGENTS.toml"
            if not config_path.exists():
                canonical = _canonical_config({}, project_name=cwd.name)
                content = render_agents_toml(canonical)
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(content, encoding="utf-8")
                actions["project_config"] = "created"
            else:
                actions["project_config"] = "exists"
        else:
            actions["project_config"] = "skipped (not in a git repo)"
    else:
        actions["project_config"] = "exists"

    # 3. Project secrets + gitignore
    project_config = find_config(Path.cwd())
    if project_config:
        config_dir = project_config.parent
        created = ensure_secrets_template(config_dir)
        if created:
            actions["project_secrets"] = "created"
            ensure_gitignore_entry(
                config_dir,
                "AGENTS.toml",
                comment="CodePilot 项目配置文件，包含项目专属设置",
            )
            ensure_gitignore_entry(
                config_dir,
                ".codepilot.secrets.toml",
                comment="CodePilot 密钥文件，包含 API Key 等敏感信息，不应提交到版本控制",
            )
            actions["gitignore"] = "updated"
        else:
            actions["project_secrets"] = "exists"

    # 4. Database
    try:
        _init_db()
        actions["database"] = "ready"
    except Exception as exc:
        actions["database"] = f"error: {exc}"

    # 5. Console encoding (Windows)
    try:
        configure_console_encoding()
        actions["console_encoding"] = "configured"
    except Exception:
        actions["console_encoding"] = "skipped"

    return actions


def _dispatch_doctor_event(
    project_info: dict | None,
    *,
    overall_ok: bool,
    results: list[CheckResult],
    fix_result: dict | None,
) -> dict | None:
    """Publish ``doctor.checked`` to enabled project event sinks."""
    if not project_info:
        return None
    try:
        from codepilot.core import event_plugins

        event = event_plugins.build_event(
            str(project_info.get("name") or ""),
            "doctor.checked",
            source="codepilot.doctor",
            event_id_prefix="doctor",
            payload={
                "ok": bool(overall_ok),
                "checks": [item.to_dict() for item in results],
                "fix": fix_result,
            },
        )
        delivery_results = event_plugins.dispatch_event_to_sinks(project_info["path"], event)
        return {
            "delivered": len([item for item in delivery_results if item.get("status") == "delivered"]),
            "results": delivery_results,
        }
    except Exception as exc:
        return {
            "delivered": 0,
            "results": [{"name": "event_sinks", "type": "event", "status": "error", "detail": str(exc)}],
        }


# ── Click command ─────────────────────────────────────────────────────────────

@click.command()
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.option("--project", "-p", help="项目名称；开启项目级配置和服务健康检查")
@click.option("--services", is_flag=True, help="包含 daemon / inspect / Web UI / Feishu 服务状态")
@click.option("--fix", "fix_mode", is_flag=True, help="一键自动修复所有配置和环境问题")
@click.pass_context
def doctor(ctx: click.Context, json_mode: bool, project: str | None, services: bool, fix_mode: bool):
    """环境自检，检查 Python、Git、AGENTS.toml、CLI 工具、数据库和编码。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    fix_result = _run_setup_fix(project) if fix_mode else None
    results = run_all_checks()
    project_info = _resolve_project(project) if (project or services or fix_mode) else None
    if project or services or fix_mode:
        results.extend(run_project_checks(project_info, include_services=True))
    overall_ok = not any(r.severity == "error" for r in results)
    event_delivery = _dispatch_doctor_event(
        project_info,
        overall_ok=overall_ok,
        results=results,
        fix_result=fix_result,
    )

    if json_mode:
        payload = {
            "checks": [r.to_dict() for r in results],
            "status_emoji": _summary_status_emoji(results),
        }
        if event_delivery is not None:
            payload["event_delivery"] = event_delivery
        if fix_result is not None:
            payload["fix"] = fix_result
        if project_info:
            payload["project"] = {
                "name": project_info.get("name"),
                "path": project_info.get("path"),
            }
        emit_json_payload("doctor", ok=overall_ok, data=payload)
        return

    # colour output
    echo()
    echo("[bold]codepilot doctor[/bold]  环境自检报告")
    echo("─" * 50)
    if fix_result is not None:
        echo("[green]已执行一键修复：[/green]")
        for action, status in fix_result.items():
            icon = "[green]✔[/green]" if "error" not in str(status) else "[red]✘[/red]"
            echo(f"  {icon}  {safe(action):28s}  {safe(status)}")
        echo("─" * 50)

    errors: list[CheckResult] = []
    warnings: list[CheckResult] = []
    for r in results:
        if r.severity == "error":
            icon = "[red]✘[/red]"
            errors.append(r)
        elif r.severity == "warning":
            icon = "[yellow]![/yellow]"
            warnings.append(r)
        else:
            icon = "[green]✔[/green]"
        echo(f"  {icon}  {safe(r.name):24s}  {safe(r.detail)}")

    echo("─" * 50)
    if not errors and not warnings:
        echo("[green]所有检查通过，环境正常。[/green]")
    else:
        if errors:
            echo(f"[yellow]发现 {len(errors)} 个错误，{len(warnings)} 个警告。[/yellow]")
        else:
            echo(f"[yellow]发现 0 个错误，{len(warnings)} 个警告。[/yellow]")
        echo()
        for r in errors:
            echo(f"  [red]✘ {safe(r.name)}[/red]")
            if r.fix:
                echo(f"    [dim]修复建议:[/dim]  {safe(r.fix)}")
        for r in warnings:
            echo(f"  [yellow]! {safe(r.name)}[/yellow]")
            if r.fix:
                echo(f"    [dim]修复建议:[/dim]  {safe(r.fix)}")
        echo()

    echo()
