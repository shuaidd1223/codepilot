"""One-command shutdown for CodePilot-owned runtime processes."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import click

from codepilot.core.output import echo, safe
from codepilot.core.runtime import (
    clear_task_runtime,
    is_process_alive,
    request_task_stop,
    stop_process_tree,
    stop_worktree_leftovers,
)
from codepilot.core.text_decode import decode_subprocess_text
from codepilot.storage import database as db


@dataclass(frozen=True)
class ServiceTarget:
    service: str
    scope: str
    pid: int


@dataclass(frozen=True)
class ActiveWork:
    daemons: list[ServiceTarget] = field(default_factory=list)
    inspections: list[ServiceTarget] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.daemons or self.inspections or self.tasks)


def _stop_webui_service() -> dict[str, Any]:
    from codepilot.commands import webui_service as webui

    raw_pid = webui._read_pid()
    targets = webui._service_targets()
    if not raw_pid and not targets:
        webui._cleanup_files()
        echo("[dim]Web UI not running[/dim]")
        return {"stopped": False, "pids": []}
    if not targets:
        webui._cleanup_files()
        echo(f"[dim]Web UI state cleared; PID={raw_pid} is no longer running[/dim]")
        return {"stopped": False, "pids": []}

    failures: list[int] = []
    for pid in targets:
        if webui.is_process_alive(pid) and not webui._send_stop(pid):
            failures.append(int(pid))
    survivors = webui._remaining_service_pids(targets)
    if survivors:
        raise click.ClickException(f"Failed to stop Web UI PID={','.join(str(pid) for pid in survivors)}")
    webui._cleanup_files()
    echo(f"[green]Web UI stopped[/green]  PID={','.join(str(pid) for pid in targets)}")
    return {"stopped": True, "pids": targets, "failures": failures}


def _stop_feishu_service() -> dict[str, Any]:
    from codepilot.commands import feishu as feishu

    status = feishu._service_status()
    pid = int(status.get("pid") or 0)
    if not pid:
        killed = feishu._stop_feishu_processes()
        feishu._clear_state()
        if killed:
            echo(f"[green]Feishu stale processes cleaned[/green]  PID={','.join(str(item) for item in killed)}")
            return {"stopped": True, "pids": killed}
        echo("[dim]Feishu service not running[/dim]")
        return {"stopped": False, "pids": []}

    feishu._mark_stopping(pid)
    if not feishu.stop_process_tree(pid, wait_seconds=5):
        raise click.ClickException(f"Failed to stop Feishu service PID={pid}")
    killed = feishu._stop_feishu_processes()
    feishu._clear_state()
    pids = [pid] + [item for item in killed if int(item) != pid]
    echo(f"[green]Feishu service stopped[/green]  PID={','.join(str(item) for item in pids)}")
    return {"stopped": True, "pids": pids}


def _is_webhook_process_command(command_line: str) -> bool:
    text = " ".join(str(command_line or "").strip().split()).lower()
    if not text:
        return False
    return bool(re.search(r"(?:-m\s+)?codepilot(?:\.exe)?\s+webhook(?:\s|$)", text))


def _windows_webhook_processes() -> list[dict[str, Any]]:
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and "
        "$_.CommandLine -match '(?:-m\\s+)?codepilot(?:\\\\.exe)?\\s+webhook(?:\\s|$)' } | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", script],
            capture_output=True,
            text=False,
            timeout=20,
        )
    except Exception:
        return []
    raw = decode_subprocess_text(result.stdout).strip()
    if result.returncode != 0 or not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    processes: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId") or 0)
        except Exception:
            pid = 0
        command_line = str(row.get("CommandLine") or "")
        if pid > 0 and _is_webhook_process_command(command_line):
            processes.append({"pid": pid, "command_line": command_line})
    return processes


def _posix_webhook_processes() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    processes: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        pid_text, _, command_line = raw.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid > 0 and _is_webhook_process_command(command_line):
            processes.append({"pid": pid, "command_line": command_line})
    return processes


def _webhook_processes() -> list[dict[str, Any]]:
    if platform.system().lower() == "windows":
        return _windows_webhook_processes()
    return _posix_webhook_processes()


def _stop_webhook_service() -> dict[str, Any]:
    current_pid = os.getpid()
    stopped: list[int] = []
    for process in _webhook_processes():
        try:
            pid = int(process.get("pid") or 0)
        except Exception:
            continue
        if pid <= 0 or pid == current_pid or not is_process_alive(pid):
            continue
        if stop_process_tree(pid, wait_seconds=5):
            stopped.append(pid)
    if stopped:
        echo(f"[green]Webhook service stopped[/green]  PID={','.join(str(item) for item in stopped)}")
    else:
        echo("[dim]Webhook service not running[/dim]")
    return {"stopped": bool(stopped), "pids": stopped}


def _service_targets(service: str, *, project: str | None = None) -> list[ServiceTarget]:
    targets: list[ServiceTarget] = []
    for state in db.list_service_states(service):
        scope = str(state.get("scope") or "")
        if project and scope != project:
            continue
        try:
            pid = int(state.get("pid") or 0)
        except Exception:
            pid = 0
        if pid and is_process_alive(pid):
            targets.append(ServiceTarget(service=service, scope=scope, pid=pid))
        elif pid:
            db.clear_service_state(service, scope)
    return targets


def _collect_active_work(project: str | None = None) -> ActiveWork:
    return ActiveWork(
        daemons=_service_targets("daemon", project=project),
        inspections=_service_targets("inspect", project=project),
        tasks=db.list_tasks(project=project, status="in_progress"),
    )


def _request_graceful_stop(work: ActiveWork) -> None:
    from codepilot.commands.daemon import stop_daemon_service
    from codepilot.commands.inspect import request_inspect_service_stop

    for target in work.daemons:
        if not target.scope or target.scope == "_global":
            db.upsert_service_state(
                "daemon",
                target.scope,
                pid=target.pid,
                status="stopping",
                meta={"pid": target.pid, "stop_requested_at": datetime.now().isoformat(timespec="seconds")},
            )
            continue
        try:
            stop_daemon_service(target.scope)
        except Exception as exc:
            echo(f"[yellow]Failed to request daemon stop for {safe(target.scope)}: {safe(exc)}[/yellow]")

    for target in work.inspections:
        if not target.scope or target.scope == "_global":
            continue
        try:
            request_inspect_service_stop(target.scope)
        except Exception as exc:
            echo(f"[yellow]Failed to request inspect stop for {safe(target.scope)}: {safe(exc)}[/yellow]")


def _force_stop_active_work(work: ActiveWork) -> None:
    reason = "force-stopped by codepilot shutdown"
    for target in [*work.daemons, *work.inspections]:
        if target.pid and is_process_alive(target.pid):
            stop_process_tree(target.pid, wait_seconds=5)
        db.clear_service_state(target.service, target.scope)
        echo(f"[yellow]{target.service} force-stopped[/yellow]  scope={safe(target.scope)} PID={target.pid}")

    for task in work.tasks:
        task_id = int(task["id"])
        request_task_stop(task_id, reason)
        pid = task.get("active_pid")
        if pid and is_process_alive(pid):
            stop_process_tree(int(pid), wait_seconds=5)
        refreshed = db.get_task(task_id) or task
        clear_task_runtime(
            task_id,
            status="cancelled",
            completed_at=refreshed.get("completed_at") or datetime.now().isoformat(),
            error_message=reason,
            stop_requested=0,
            stop_reason=None,
        )
        wt = (refreshed or task).get("worktree_path")
        project_path = (refreshed or task).get("project_path")
        try:
            if wt and wt != project_path:
                stop_worktree_leftovers(wt, wait_seconds=3)
        except Exception:
            pass
        echo(f"[yellow]Task #{task_id} cancelled[/yellow]  {safe(task.get('title') or '')}")


def _wait_for_idle(project: str | None = None, poll_interval: float = 2.0) -> None:
    while True:
        work = _collect_active_work(project)
        if not work.any():
            return
        time.sleep(max(0.2, float(poll_interval)))


def _active_summary(work: ActiveWork) -> str:
    parts = []
    if work.daemons:
        parts.append(f"daemon={len(work.daemons)}")
    if work.inspections:
        parts.append(f"inspect={len(work.inspections)}")
    if work.tasks:
        parts.append(f"tasks={len(work.tasks)}")
    return ", ".join(parts) if parts else "none"


@click.command("shutdown")
@click.option("--project", "-p", default=None, help="Limit shutdown checks to one registered project.")
@click.option("--force", is_flag=True, help="Force stop active work without prompting.")
@click.option("--poll-interval", type=float, default=2.0, show_default=True, help="Seconds between idle checks in wait mode.")
def shutdown(project: str | None, force: bool, poll_interval: float) -> None:
    """Stop CodePilot services and optionally force-stop active work."""
    db.init_db()
    _stop_webui_service()
    _stop_feishu_service()
    _stop_webhook_service()

    work = _collect_active_work(project)
    if not work.any():
        echo("[green]CodePilot shutdown complete; no active work remains.[/green]")
        return

    echo(f"[yellow]Active CodePilot work detected:[/yellow] {_active_summary(work)}")
    should_force = bool(force)
    if not should_force:
        answer = click.prompt(
            "Enter Y to force stop known CodePilot PIDs; any other answer waits",
            default="",
            show_default=False,
        )
        should_force = answer.strip() == "Y"

    if should_force:
        _force_stop_active_work(work)
        echo("[green]CodePilot shutdown complete; active work was force-stopped.[/green]")
        return

    echo("[cyan]waiting for active work to finish...[/cyan]")
    _request_graceful_stop(work)
    _wait_for_idle(project, poll_interval=poll_interval)
    echo("[green]CodePilot shutdown complete; active work finished.[/green]")
