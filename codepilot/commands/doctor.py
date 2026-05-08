"""codepilot doctor — environment self-check command."""

from __future__ import annotations
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.output import echo, safe
from codepilot.storage import database as db


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
    results: list[CheckResult] = []
    install_hints = {
        "codex": "npm install -g @openai/codex",
        "claude": "npm install -g @anthropic-ai/claude-code",
        # OpenCode is the bottom-tier fallback; absence is a warning, not an error.
        "opencode": "npm install -g opencode-ai (or visit https://opencode.ai)",
    }
    for tool in ("codex", "claude", "opencode"):
        found = shutil.which(tool)
        if found:
            results.append(CheckResult(f"cli_{tool}", True, f"{tool} -> {found}"))
        else:
            results.append(CheckResult(
                f"cli_{tool}", False, f"'{tool}' 不在 PATH 中",
                fix=install_hints.get(tool, f"install {tool}"),
            ))
    return results


def _check_api_keys() -> list[CheckResult]:
    """Check API key availability for commonly used providers."""
    from codepilot.ai_support.service import API_PROVIDERS, normalize_agent_name
    from codepilot.ai_support.providers import resolve_api_provider
    from codepilot.core.config import load_config

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
    results.append(_feishu_config_check(project_info))
    if not include_services and not project_info:
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


def _run_setup_fix(project: str | None) -> dict:
    """Run the conservative project setup fix used by ``doctor --fix``."""
    db.init_db()
    project_info = db.get_project(project) if project else db.find_project_by_path(Path.cwd())
    target = Path(project_info["path"]).resolve() if project_info else Path.cwd().resolve()
    project_name = project or (str(project_info.get("name") or "") if project_info else None)

    from codepilot.commands.setup import setup_project

    return setup_project(target, project_name, dry_run=False)


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
@click.option("--fix", "fix_mode", is_flag=True, help="执行保守自动修复：运行项目级 setup，不修改真实 Codex hooks")
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
        echo("[green]已执行保守修复：项目级 setup 完成，真实 .codex/hooks.json 未修改。[/green]")
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

