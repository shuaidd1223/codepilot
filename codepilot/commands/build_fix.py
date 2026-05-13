"""build-fix quality loop entrypoint."""

from __future__ import annotations

import contextlib
import io
import subprocess
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.commands.run_orchestrator import run_backlog
from codepilot.commands.status import _resolve_project
from codepilot.core.output import echo
from codepilot.core.text_decode import decode_subprocess_text
from codepilot.storage import database as db


class BuildFixError(RuntimeError):
    """Raised for user-facing build-fix precondition errors."""


def _select_task(project: str, task_id: int | None) -> dict:
    if task_id:
        task = db.get_task(task_id)
        if not task:
            raise BuildFixError(f"任务 #{task_id} 不存在")
        if task.get("project") != project:
            raise BuildFixError(f"任务 #{task_id} 不属于项目 {project}")
        return task

    failed = db.list_tasks(project=project, status="failed")
    if not failed:
        raise BuildFixError(f"项目 {project} 没有 failed 任务可修复")
    return sorted(failed, key=lambda item: int(item.get("id") or 0))[0]


def _latest_failure_log(task_id: int) -> dict[str, Any]:
    logs = db.list_task_logs(task_id)
    if not logs:
        return {"count": 0, "latest": None, "tail": ""}
    latest = logs[-1]
    output = str(latest.get("output") or "")
    tail = "\n".join(output.splitlines()[-40:]) if output else ""
    return {"count": len(logs), "latest": latest, "tail": tail[-4000:]}


def _extract_verification_commands(content: str) -> list[str]:
    commands: list[str] = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "Verification" in line or ":---" in line:
            continue
        columns = [part.strip() for part in line.strip("|").split("|")]
        if len(columns) < 3:
            continue
        command = columns[2].strip().strip("`")
        if not command or command.lower() in {"-", "n/a", "none", "待补充", "manual", "手工验证"}:
            continue
        commands.append(command)
    return commands


def _verification_commands(task: dict, explicit_commands: tuple[str, ...]) -> list[str]:
    commands = [str(item).strip() for item in explicit_commands if str(item).strip()]
    if commands:
        return commands
    return _extract_verification_commands(str(task.get("content") or ""))


def _run_verification(project_path: Path, commands: list[str], timeout_seconds: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=str(project_path),
                shell=True,
                capture_output=True,
                text=False,
                timeout=max(1, int(timeout_seconds or 1)),
            )
            stdout = decode_subprocess_text(completed.stdout)[-4000:]
            stderr = decode_subprocess_text(completed.stderr)[-4000:]
            results.append(
                {
                    "command": command,
                    "exit_code": completed.returncode,
                    "ok": completed.returncode == 0,
                    "stdout": stdout,
                    "stderr": stderr,
                    "timed_out": False,
                }
            )
        except subprocess.TimeoutExpired as exc:
            results.append(
                {
                    "command": command,
                    "exit_code": None,
                    "ok": False,
                    "stdout": decode_subprocess_text(exc.stdout or b"")[-4000:],
                    "stderr": decode_subprocess_text(exc.stderr or b"")[-4000:],
                    "timed_out": True,
                }
            )
    return results


def _run_backlog_quietly(project: str, *, json_mode: bool, executor: str, auto_commit: bool) -> dict:
    if not json_mode:
        return run_backlog(
            project,
            once=True,
            limit=1,
            retry_on_failure=False,
            quiet=True,
            executor=executor,
            auto_commit=auto_commit,
        )
    with contextlib.redirect_stdout(io.StringIO()):
        return run_backlog(
            project,
            once=True,
            limit=1,
            retry_on_failure=False,
            quiet=True,
            executor=executor,
            auto_commit=auto_commit,
        )


def run_build_fix(
    project: str,
    *,
    task_id: int | None = None,
    verify_commands: tuple[str, ...] = (),
    timeout_seconds: int = 300,
    executor: str = "auto",
    auto_commit: bool = True,
    dry_run: bool = False,
    json_mode: bool = False,
) -> dict[str, Any]:
    db.init_db()
    project_record = db.get_project(project)
    if not project_record:
        raise BuildFixError(f"项目 {project} 未注册")
    project_path = Path(project_record["path"]).resolve()
    task = _select_task(project, task_id)
    if task.get("status") == "in_progress":
        raise BuildFixError(f"任务 #{task['id']} 正在运行，不能 build-fix")

    failure_log = _latest_failure_log(int(task["id"]))
    triage = {
        "status": task.get("status"),
        "phase": task.get("run_phase") or "",
        "error_message": task.get("error_message") or "",
        "retry_count": int(task.get("retry_count") or 0),
        "max_retries": int(task.get("max_retries") or 0),
        "log": failure_log,
    }
    commands = _verification_commands(task, verify_commands)
    actions: list[dict[str, Any]] = []
    run_stats: dict[str, Any] | None = None

    if dry_run:
        actions.append({"type": "retry", "status": "would_reset"})
        actions.append({"type": "run", "status": "would_run", "executor": executor})
        verification: list[dict[str, Any]] = [
            {"command": command, "exit_code": None, "ok": None, "stdout": "", "stderr": "", "timed_out": False}
            for command in commands
        ]
        verdict = "dry_run"
    else:
        reset = db.reset_task_for_retry(int(task["id"]))
        actions.append({"type": "retry", "status": "reset", "task_status": reset.get("status")})
        run_stats = _run_backlog_quietly(project, json_mode=json_mode, executor=executor, auto_commit=auto_commit)
        actions.append({"type": "run", "status": "completed", "stats": run_stats})
        verification = _run_verification(project_path, commands, timeout_seconds) if commands else []
        refreshed = db.get_task(int(task["id"])) or reset
        if refreshed.get("status") == "done" and all(item.get("ok") for item in verification):
            verdict = "pass"
        elif refreshed.get("status") == "done" and not verification:
            verdict = "pass"
        else:
            verdict = "fail"

    return {
        "project": project,
        "task_id": int(task["id"]),
        "triage": triage,
        "actions": actions,
        "verification": verification,
        "verdict": verdict,
        "run_stats": run_stats,
    }


@click.command("build-fix")
@click.option("--project", "-p", callback=_resolve_project, required=True, help="项目名")
@click.option("--task-id", type=int, help="指定任务 ID；不指定则选择最早的 failed 任务")
@click.option("--verify-command", multiple=True, help="额外或覆盖验证命令，可重复传入")
@click.option("--timeout", "timeout_seconds", type=int, default=300, show_default=True, help="单条验证命令超时时间（秒）")
@click.option("--executor", type=click.Choice(["auto", "dispatch", "builtin"], case_sensitive=False), default="auto")
@click.option("--auto-commit/--no-auto-commit", default=True, help="传递给执行器的自动提交选项")
@click.option("--dry-run", is_flag=True, help="只输出将执行的修复闭环，不改任务状态、不运行执行器")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def build_fix(
    ctx: click.Context,
    project: str,
    task_id: int | None,
    verify_command: tuple[str, ...],
    timeout_seconds: int,
    executor: str,
    auto_commit: bool,
    dry_run: bool,
    json_mode: bool,
):
    """对 failed 任务执行“收集失败 -> 重试修复 -> 验证 -> 判定”的质量闭环。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        payload = run_build_fix(
            project,
            task_id=task_id,
            verify_commands=verify_command,
            timeout_seconds=timeout_seconds,
            executor=executor.lower(),
            auto_commit=auto_commit,
            dry_run=dry_run,
            json_mode=json_mode,
        )
    except BuildFixError as exc:
        if json_mode:
            emit_json_payload(
                "build-fix",
                ok=False,
                data={},
                error=str(exc),
                error_code="build_fix_error",
            )
            ctx.exit(1)
        raise click.ClickException(str(exc)) from exc

    if json_mode:
        emit_json_payload("build-fix", ok=payload["verdict"] in {"pass", "dry_run"}, data=payload)
        if payload["verdict"] == "fail":
            ctx.exit(1)
        return

    verdict = payload["verdict"]
    style = "green" if verdict == "pass" else ("yellow" if verdict == "dry_run" else "red")
    echo(f"[{style}]build-fix verdict: {verdict}[/{style}]  task=#{payload['task_id']}")
    if payload["verification"]:
        for item in payload["verification"]:
            mark = "OK" if item.get("ok") else "FAIL"
            click.echo(f"  [{mark}] {item['command']}")
    if verdict == "fail":
        ctx.exit(1)
