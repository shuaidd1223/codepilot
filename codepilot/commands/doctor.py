"""codepilot doctor — environment self-check command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from codepilot.output import echo, safe


# ── Check result model ───────────────────────────────────────────────────────

class CheckResult:
    """Single diagnostic check result."""

    __slots__ = ("name", "ok", "severity", "detail", "fix")

    def __init__(
        self,
        name: str,
        ok: bool,
        detail: str,
        fix: Optional[str] = None,
        *,
        severity: Optional[str] = None,
    ):
        resolved_severity = severity or ("ok" if ok else "error")
        if resolved_severity not in {"ok", "warning", "error"}:
            raise ValueError(f"unsupported severity: {resolved_severity}")
        self.name = name
        self.ok = resolved_severity != "error"
        self.severity = resolved_severity
        self.detail = detail
        self.fix = fix  # suggested remediation command / hint

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
        }
        if self.fix:
            d["fix"] = self.fix
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
    from codepilot.config import load_config
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
    from codepilot.config import find_config

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
    """codex / claude CLI on PATH."""
    results: list[CheckResult] = []
    for tool in ("codex", "claude"):
        found = shutil.which(tool)
        if found:
            results.append(CheckResult(f"cli_{tool}", True, f"{tool} -> {found}"))
        else:
            results.append(CheckResult(
                f"cli_{tool}", False, f"'{tool}' 不在 PATH 中",
                fix=f"npm install -g @openai/codex" if tool == "codex"
                else "npm install -g @anthropic-ai/claude-code",
            ))
    return results


def _check_api_keys() -> list[CheckResult]:
    """Check API key availability for commonly used providers."""
    from codepilot.ai import API_PROVIDERS, normalize_agent_name
    from codepilot.ai_providers import resolve_api_provider
    from codepilot.config import load_config

    results: list[CheckResult] = []
    cfg = load_config()
    key_groups: dict[str, dict[str, object]] = {}
    required_provider = ""

    if cfg and cfg.classifier.enabled:
        classifier_provider = normalize_agent_name((cfg.classifier.provider or "").strip())
        if classifier_provider in API_PROVIDERS:
            required_candidate = resolve_api_provider(
                classifier_provider,
                cfg.config_file_path,
            )
            if required_candidate.requires_api_key():
                required_provider = classifier_provider

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
            severity="warning",
        ))

    return results


def _check_db_path() -> CheckResult:
    """Task DB path readable & writable."""
    from codepilot.db import _get_db_path

    db_path = _get_db_path()
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
    results.extend(_check_api_keys())
    results.append(_check_db_path())
    results.append(_check_console_encoding())
    return results


# ── Click command ─────────────────────────────────────────────────────────────

@click.command()
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def doctor(ctx: click.Context, json_mode: bool):
    """环境自检，检查 Python、Git、AGENTS.toml、CLI 工具、数据库和编码。"""
    if not json_mode and ctx.parent:
        json_mode = (ctx.parent.obj or {}).get("json_mode", False)
    results = run_all_checks()

    if json_mode:
        payload = {
            "checks": [r.to_dict() for r in results],
            "ok": not any(r.severity == "error" for r in results),
            "status_emoji": _summary_status_emoji(results),
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    # colour output
    echo()
    echo("[bold]codepilot doctor[/bold]  环境自检报告")
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
