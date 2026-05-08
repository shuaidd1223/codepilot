"""Project-local provider-neutral execution smoke command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core import event_plugins
from codepilot.core.output import echo
from codepilot.storage import database as db


SUPPORTED_PROVIDERS = ("codex", "claude", "opencode", "gemini", "custom")
DEFAULT_PROVIDER_COMMANDS = {
    "codex": "codex",
    "claude": "claude",
    "opencode": "opencode",
    "gemini": "gemini",
}
PROVIDER_ENV_HINTS = {
    "codex": ("OPENAI_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY",),
    # OpenCode auto-picks ANTHROPIC_API_KEY > OPENAI_API_KEY > DeepSeek (OpenAI-compatible).
    "opencode": ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}
EXEC_LOG_RELATIVE_PATH = Path(".codepilot") / "exec" / "exec.jsonl"


class ExecCommandError(ValueError):
    """Raised when project-local exec cannot safely continue."""


def _resolve_project(project: str | None) -> dict:
    db.init_db()
    if project:
        found = db.get_project(project)
        if not found:
            raise click.ClickException(f"项目 '{project}' 未注册。")
        return found
    found = db.find_project_by_path(Path.cwd())
    if not found:
        raise click.ClickException("当前目录不属于已注册项目；请使用 -p/--project 指定项目。")
    return found


def _resolve_cwd(project_root: Path, cwd: str | None) -> Path:
    if not cwd:
        return project_root
    candidate = Path(cwd).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ExecCommandError("--cwd 必须位于当前项目目录内。") from exc
    if not resolved.is_dir():
        raise ExecCommandError(f"--cwd 不是目录：{resolved}")
    return resolved


def _tail(text: str, max_lines: int) -> str:
    lines = (text or "").splitlines()
    selected = lines[-max(1, int(max_lines or 1)) :]
    return "\n".join(selected).strip()


def _provider_executable(provider: str, command_parts: tuple[str, ...]) -> str:
    if provider == "custom" and command_parts:
        return command_parts[0]
    return DEFAULT_PROVIDER_COMMANDS.get(provider, command_parts[0] if command_parts else "")


def _preflight(project_info: dict, provider: str, command_parts: tuple[str, ...], cwd: Path) -> dict[str, Any]:
    executable = _provider_executable(provider, command_parts)
    resolved_executable = shutil.which(executable) if executable else None
    env_groups = PROVIDER_ENV_HINTS.get(provider, ())
    missing_env = []
    if env_groups and not any(os.environ.get(name) for name in env_groups):
        missing_env = list(env_groups)
    return {
        "project_configured": bool(project_info.get("config_file")) and Path(str(project_info.get("config_file"))).exists(),
        "cwd_exists": cwd.is_dir(),
        "provider_executable": executable,
        "provider_executable_found": bool(resolved_executable),
        "provider_executable_path": resolved_executable or "",
        "missing_env": missing_env,
    }


def _build_exec_event(project_name: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return event_plugins.build_event(
        project_name,
        event_type,
        source="codepilot.exec",
        payload=payload,
        event_id_prefix="exec",
    )


def _dispatch(project_root: Path, event: dict[str, Any]) -> None:
    try:
        event_plugins.dispatch_event_to_sinks(project_root, event)
    except Exception:
        return


def _append_exec_log(project_root: Path, payload: dict[str, Any]) -> Path:
    path = project_root / EXEC_LOG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return path


def run_exec(
    project_info: dict,
    *,
    provider: str,
    command_parts: tuple[str, ...],
    cwd: str | None,
    dry_run: bool,
    timeout_seconds: int,
    tail_lines: int,
) -> dict[str, Any]:
    provider = str(provider or "custom").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ExecCommandError(f"不支持的 provider：{provider}")
    project_root = Path(project_info["path"]).resolve()
    resolved_cwd = _resolve_cwd(project_root, cwd)
    command = [str(item) for item in command_parts if str(item)]
    preflight = _preflight(project_info, provider, tuple(command), resolved_cwd)
    base_payload: dict[str, Any] = {
        "project": project_info["name"],
        "provider": provider,
        "agent_runtime": provider,
        "cwd": str(resolved_cwd),
        "command": command,
        "dry_run": bool(dry_run),
        "preflight": preflight,
    }
    if dry_run:
        return {**base_payload, "exit_code": None, "duration_ms": 0, "stdout_tail": "", "stderr_tail": "", "verdict": "dry_run"}
    if not command:
        raise ExecCommandError("非 dry-run 执行必须提供命令。")

    started_payload = {key: base_payload[key] for key in ("provider", "agent_runtime", "cwd", "command")}
    _dispatch(project_root, _build_exec_event(project_info["name"], "exec.started", started_payload))
    started = time.perf_counter()
    timed_out = False
    error = ""
    try:
        completed = subprocess.run(
            command,
            cwd=str(resolved_cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout_seconds or 1)),
            shell=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        error = f"timeout after {timeout_seconds}s"
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)
        error = str(exc)
    duration_ms = int((time.perf_counter() - started) * 1000)
    verdict = "pass" if exit_code == 0 and not timed_out else "fail"
    result = {
        **base_payload,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout_tail": _tail(stdout, tail_lines),
        "stderr_tail": _tail(stderr, tail_lines),
        "timed_out": timed_out,
        "error": error,
        "verdict": verdict,
    }
    log_path = _append_exec_log(project_root, result)
    result["log_path"] = str(log_path)
    event_type = "exec.completed" if verdict == "pass" else "exec.failed"
    _dispatch(project_root, _build_exec_event(project_info["name"], event_type, result))
    return result


@click.command("exec", context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "allow_interspersed_args": False})
@click.argument("command_parts", nargs=-1, type=click.UNPROCESSED)
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--provider", type=click.Choice(SUPPORTED_PROVIDERS, case_sensitive=False), default="custom", show_default=True)
@click.option("--cwd", "cwd", help="项目内工作目录，默认项目根目录")
@click.option("--dry-run", is_flag=True, help="只做 provider/cwd/config preflight，不执行命令")
@click.option("--timeout", "timeout_seconds", type=int, default=300, show_default=True, help="命令超时秒数")
@click.option("--tail-lines", type=int, default=40, show_default=True, help="stdout/stderr tail 行数")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def exec_cmd(
    ctx: click.Context,
    command_parts: tuple[str, ...],
    project: str | None,
    provider: str,
    cwd: str | None,
    dry_run: bool,
    timeout_seconds: int,
    tail_lines: int,
    json_mode: bool,
) -> None:
    """项目内多 provider 命令烟测和审计执行，不安装全局 wrapper。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        data = run_exec(
            project_info,
            provider=provider,
            command_parts=command_parts,
            cwd=cwd,
            dry_run=dry_run,
            timeout_seconds=timeout_seconds,
            tail_lines=tail_lines,
        )
    except (ExecCommandError, click.ClickException) as exc:
        if json_mode:
            emit_json_payload("exec", ok=False, data={}, error=str(exc), error_code="exec_error")
            ctx.exit(1)
        raise click.ClickException(str(exc)) from exc
    if json_mode:
        emit_json_payload("exec", ok=data.get("verdict") in {"pass", "dry_run"}, data=data)
        if data.get("verdict") == "fail":
            ctx.exit(1)
        return
    echo(f"[green]exec {data['verdict']}[/green] provider={data['provider']} exit={data['exit_code']}")
