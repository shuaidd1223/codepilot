"""Service lifecycle helpers for the `inspect` command."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from codepilot.core.runtime import codepilot_command
from codepilot.core.service_launcher import DetachedProcessHandle, append_log_header, spawn_detached_command_via_launcher
from codepilot.core.paths import _slugify_project_name, global_storage_root

INSPECT_STATE_DIR = global_storage_root() / "inspect"


def service_log_path(project: str, *, state_dir: Path = INSPECT_STATE_DIR) -> Path:
    root = state_dir / _slugify_project_name(project)
    return root / "inspect.log"


def inspect_service_status(
    project: str,
    *,
    state_dir: Path = INSPECT_STATE_DIR,
    get_service_state: Callable[[str, str], dict | None],
    is_alive: Callable[[int], bool],
) -> dict:
    state = get_service_state("inspect", project)
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    try:
        pid = int(state.get("pid") or 0) if state else 0
    except Exception:
        pid = 0
    running = bool(pid and is_alive(pid))
    return {
        "running": running,
        "pid": pid if running else 0,
        "project": project,
        "started_at": meta.get("started_at") or "",
        "log": str(state.get("log_path") or service_log_path(project, state_dir=state_dir)) if state else str(service_log_path(project, state_dir=state_dir)),
    }


def cleanup_inspect_files(
    project: str,
    *,
    clear_service_state: Callable[[str, str], None],
) -> None:
    clear_service_state("inspect", project)


def write_inspect_meta(
    project: str,
    pid: int,
    *,
    interval: int,
    planner: str,
    agent: str,
    state_dir: Path = INSPECT_STATE_DIR,
    upsert_service_state: Callable[..., None],
    now_iso_fn: Callable[[], str],
) -> None:
    payload = {
        "pid": int(pid),
        "project": project,
        "interval": int(interval),
        "planner": planner,
        "agent": agent,
        "started_at": now_iso_fn(),
    }
    upsert_service_state(
        "inspect",
        project,
        pid=int(pid),
        status="running",
        log_path=str(service_log_path(project, state_dir=state_dir)),
        heartbeat_at=now_iso_fn(),
        meta=payload,
    )


def spawn_detached_inspect(
    project: str,
    *,
    max_new: int | None,
    dry_run: bool,
    agent: str,
    planner: str | None,
    interval: int | None,
    state_dir: Path = INSPECT_STATE_DIR,
    now_iso_fn: Callable[[], str],
) -> DetachedProcessHandle:
    log_file = service_log_path(project, state_dir=state_dir)
    append_log_header(log_file, f"\n--- start {now_iso_fn()} project={project} ---\n")

    cmd = codepilot_command(
        "inspect",
        "--project",
        project,
        "--foreground",
        "--agent",
        agent,
    )
    if max_new is not None:
        cmd.extend(["--max", str(max_new)])
    if dry_run:
        cmd.append("--dry-run")
    if planner:
        cmd.extend(["--planner", planner])
    if interval is not None:
        cmd.extend(["--interval", str(interval)])

    pid = spawn_detached_command_via_launcher(cmd, log_file=log_file)
    return DetachedProcessHandle(pid)
