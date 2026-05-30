"""Lifecycle orchestration helpers for the ``inspect`` command."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class InspectServiceLifecycleOptions:
    project: str
    max_new: int | None
    dry_run: bool
    agent: str
    planner: str | None
    interval: int | None
    once: bool
    foreground: bool
    json_mode: bool
    show_status: bool
    stop_service: bool


@dataclass(frozen=True)
class ForegroundInspectLoopOptions:
    project: str
    project_info: dict
    signals: tuple[str, ...]
    max_new_tasks: int
    interval_seconds: int
    planner: str
    priority: str
    auto_execute: bool
    agent: str
    dry_run: bool
    once: bool
    json_mode: bool


def handle_service_lifecycle(
    options: InspectServiceLifecycleOptions,
    *,
    inspect_service_status_fn: Callable[[str], dict],
    start_inspect_service_fn: Callable[..., dict],
    stop_inspect_service_fn: Callable[[str], dict],
    emit_json_payload_fn: Callable[..., None],
    echo_fn: Callable[[str], None],
) -> tuple[bool, int | None]:
    """Handle status/stop/background-start branches for ``inspect`` command."""
    project = options.project
    if options.show_status:
        status = inspect_service_status_fn(project)
        if options.json_mode:
            emit_json_payload_fn("inspect", ok=True, data={"action": "status", "service": status})
        else:
            if status["running"]:
                echo_fn(f"[green]巡检运行中[/green]  PID={status['pid']}  项目={project}")
                echo_fn(f"[dim]日志: {status['log']}[/dim]")
            else:
                echo_fn(f"[dim]项目 {project} 巡检未运行[/dim]")
        return True, None

    if options.stop_service:
        try:
            result = stop_inspect_service_fn(project)
        except RuntimeError as exc:
            if options.json_mode:
                emit_json_payload_fn(
                    "inspect",
                    ok=False,
                    data={"action": "stop", "service": inspect_service_status_fn(project)},
                    error=str(exc),
                    error_code="stop_failed",
                )
                return True, 1
            raise
        if options.json_mode:
            emit_json_payload_fn(
                "inspect",
                ok=bool(result.get("stopped")),
                data={"action": "stop", "result": result},
                error=None if result.get("stopped") else f"项目 {project} 巡检未运行",
                error_code=None if result.get("stopped") else "service_not_running",
            )
        else:
            if result["stopped"]:
                echo_fn(f"[green]项目 {project} 巡检已停止[/green]  PID={','.join(str(pid) for pid in result['pids'])}")
            else:
                echo_fn(f"[dim]项目 {project} 巡检未运行[/dim]")
        return True, None

    if not options.once and not options.foreground and not options.json_mode:
        start_kwargs = {
            "max_new": options.max_new,
            "dry_run": options.dry_run,
            "agent": options.agent,
            "planner": options.planner,
            "interval": options.interval,
        }
        result = start_inspect_service_fn(project, **start_kwargs)
        if result["started"]:
            echo_fn(f"[green]项目 {project} 巡检已后台启动[/green]  PID={result['pid']}")
        else:
            echo_fn(f"[yellow]项目 {project} 巡检已在运行[/yellow]  PID={result['pid']}")
        echo_fn(f"[dim]日志: {result['log']}[/dim]")
        return True, None

    return False, None


def run_foreground_inspection_loop(
    options: ForegroundInspectLoopOptions,
    *,
    touch_service_state_fn: Callable[..., None],
    clear_service_state_fn: Callable[[str, str], None],
    service_log_path_fn: Callable[[str], str],
    print_round_header_fn: Callable[..., None],
    run_inspection_fn: Callable[..., dict],
    emit_inspection_result_fn: Callable[..., None],
    echo_fn: Callable[[str], None],
    inspect_stop_requested_fn: Callable[[str], bool] | None = None,
    get_pid_fn: Callable[[], int] = os.getpid,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Run foreground inspect rounds and keep runtime state lifecycle consistent."""
    round_num = 0

    def _stop_requested() -> bool:
        return bool(inspect_stop_requested_fn and inspect_stop_requested_fn(options.project))

    def _sleep_or_stop(seconds: int) -> bool:
        if inspect_stop_requested_fn is None:
            sleep_fn(seconds)
            return False
        remaining = float(max(0, int(seconds)))
        while remaining > 0:
            if _stop_requested():
                return True
            chunk = min(1.0, remaining)
            sleep_fn(chunk)
            remaining -= chunk
        return _stop_requested()

    try:
        while True:
            if _stop_requested():
                if not options.json_mode:
                    echo_fn("[yellow]收到巡检停止请求，退出；当前轮已完成。[/yellow]")
                break
            touch_service_state_fn(
                "inspect",
                options.project,
                pid=get_pid_fn(),
                log_path=service_log_path_fn(options.project),
                status="running",
            )
            round_num += 1
            if not options.json_mode:
                print_round_header_fn(
                    project_name=options.project_info["name"],
                    planner=options.planner,
                    agent=options.agent,
                    max_new_tasks=options.max_new_tasks,
                    signals=options.signals,
                    once=options.once,
                    round_num=round_num,
                )

            result = run_inspection_fn(
                options.project_info,
                max_new_tasks=options.max_new_tasks,
                signals=options.signals,
                auto_execute=options.auto_execute,
                priority=options.priority,
                agent=options.agent,
                planner=options.planner,
                dry_run=options.dry_run,
            )
            emit_inspection_result_fn(result, dry_run=options.dry_run, json_mode=options.json_mode)

            if options.once:
                break
            if _stop_requested():
                if not options.json_mode:
                    echo_fn("[yellow]收到巡检停止请求，退出；当前轮已完成。[/yellow]")
                break
            if not options.json_mode:
                echo_fn(f"[dim]下次巡检将在 {options.interval_seconds} 秒后...[/dim]")
            try:
                if _sleep_or_stop(options.interval_seconds):
                    if not options.json_mode:
                        echo_fn("[yellow]收到巡检停止请求，退出。[/yellow]")
                    break
            except KeyboardInterrupt:
                if not options.json_mode:
                    echo_fn("[yellow]巡检已停止[/yellow]")
                break
    finally:
        clear_service_state_fn("inspect", options.project)
