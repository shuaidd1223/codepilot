"""Daemon integration for scheduled and event-triggered agent jobs."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from codepilot.core import event_plugins
from codepilot.core.config import AgentsConfig, load_project_config
from codepilot.scheduled.runner import AgentJobResult, SubprocessRun, run_agent_job
from codepilot.scheduled.triggers import build_event_agent_jobs, build_scheduled_agent_jobs


DAEMON_EVENT_SINK_NAME = "scheduled-agent-daemon"
DAEMON_EVENT_SINK_PATH = ".codepilot/events/daemon-agent-events.jsonl"
STATE_RELATIVE_PATH = Path(".codepilot") / "scheduled" / "daemon-state.json"
SUPPORTED_EVENT_TYPES = {"task.failed", "task.completed", "commit", "feishu_message", "task.updated"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _project_name(project: Mapping[str, Any]) -> str:
    return str(project.get("name") or project.get("project") or "").strip()


def _project_root(project: Mapping[str, Any]) -> Path:
    path = str(project.get("path") or project.get("project_path") or "").strip()
    if not path:
        raise ValueError("project path is required for scheduled daemon jobs")
    return Path(path).expanduser().resolve()


def _state_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / STATE_RELATIVE_PATH


def _load_state(project_root: str | Path) -> dict[str, Any]:
    path = _state_path(project_root)
    if not path.is_file():
        return {"schema_version": 1, "last_run_at": {}, "event_offsets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    last_run_at = data.get("last_run_at")
    event_offsets = data.get("event_offsets")
    return {
        "schema_version": 1,
        "last_run_at": last_run_at if isinstance(last_run_at, dict) else {},
        "event_offsets": event_offsets if isinstance(event_offsets, dict) else {},
    }


def _save_state(project_root: str | Path, state: dict[str, Any]) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _last_run_map(state: Mapping[str, Any]) -> dict[str, datetime]:
    raw = state.get("last_run_at")
    if not isinstance(raw, Mapping):
        return {}
    parsed: dict[str, datetime] = {}
    for name, value in raw.items():
        timestamp = _parse_datetime(value)
        if timestamp is not None:
            parsed[str(name)] = timestamp
    return parsed


def ensure_daemon_event_sink(project_root: str | Path) -> dict[str, Any]:
    """Ensure the daemon has an enabled JSONL event sink to consume."""

    return event_plugins.register_jsonl_sink(
        project_root,
        name=DAEMON_EVENT_SINK_NAME,
        path=DAEMON_EVENT_SINK_PATH,
        events=sorted(SUPPORTED_EVENT_TYPES),
        enabled=True,
    )


def _event_log_path(project_root: str | Path) -> Path:
    root = Path(project_root).expanduser().resolve()
    return root / DAEMON_EVENT_SINK_PATH


def _read_new_sink_events(project_root: str | Path, state: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    path = _event_log_path(project_root)
    offsets = state.setdefault("event_offsets", {})
    key = DAEMON_EVENT_SINK_PATH
    try:
        offset = int(offsets.get(key) or 0) if isinstance(offsets, dict) else 0
    except (TypeError, ValueError):
        offset = 0
    if not path.is_file():
        return [], 0

    lines = path.read_text(encoding="utf-8").splitlines()
    if offset > len(lines):
        offset = 0
    events: list[dict[str, Any]] = []
    for line in lines[offset:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events, len(lines)


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("type") or event.get("event_type") or "").strip()
    if event_type not in SUPPORTED_EVENT_TYPES:
        return None
    normalized = dict(event)
    payload = normalized.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    if event_type == "task.updated":
        status = str(payload.get("status") or "").strip().lower()
        if status == "failed":
            normalized["type"] = "task.failed"
        elif status in {"done", "completed"}:
            normalized["type"] = "task.completed"
        else:
            return None
        normalized["payload"] = payload
    return normalized


def _run_jobs(
    jobs: list[dict[str, Any]],
    *,
    project_root: Path,
    config: AgentsConfig,
    dry_run: bool,
    subprocess_run: SubprocessRun,
    now: datetime,
) -> list[AgentJobResult]:
    results: list[AgentJobResult] = []
    for job in jobs:
        results.append(
            run_agent_job(
                job,
                project_root=project_root,
                dry_run=dry_run,
                subprocess_run=subprocess_run,
                commands=config.commands,
                now=now,
            )
        )
    return results


def run_scheduled_agent_jobs(
    project: Mapping[str, Any],
    config: AgentsConfig,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    subprocess_run: SubprocessRun = subprocess.run,
) -> dict[str, Any]:
    """Build and run due scheduled agent jobs for one project."""

    current_time = now or _now()
    project_root = _project_root(project)
    state = _load_state(project_root)
    jobs = build_scheduled_agent_jobs(
        config.automation.scheduled_agents,
        project=_project_name(project),
        now=current_time,
        last_run_at=_last_run_map(state),
    )
    results = _run_jobs(
        jobs,
        project_root=project_root,
        config=config,
        dry_run=dry_run,
        subprocess_run=subprocess_run,
        now=current_time,
    )
    if jobs and not dry_run:
        last_run_at = state.setdefault("last_run_at", {})
        if isinstance(last_run_at, dict):
            for job in jobs:
                last_run_at[str(job.get("name") or "")] = current_time.isoformat()
            _save_state(project_root, state)
    return {"job_count": len(jobs), "jobs": jobs, "results": results}


def run_event_agent_jobs(
    project: Mapping[str, Any],
    config: AgentsConfig,
    *,
    events: list[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    subprocess_run: SubprocessRun = subprocess.run,
) -> dict[str, Any]:
    """Build and run event-triggered agent jobs for one project."""

    current_time = now or _now()
    project_root = _project_root(project)
    state = _load_state(project_root)
    next_offset: int | None = None
    if events is None:
        ensure_daemon_event_sink(project_root)
        raw_events, next_offset = _read_new_sink_events(project_root, state)
    else:
        raw_events = [dict(item) for item in events]

    normalized_events = [item for item in (_normalize_event(event) for event in raw_events) if item is not None]
    jobs: list[dict[str, Any]] = []
    for event in normalized_events:
        jobs.extend(
            build_event_agent_jobs(
                config.automation.event_agents,
                event,
                project=_project_name(project),
            )
        )

    results = _run_jobs(
        jobs,
        project_root=project_root,
        config=config,
        dry_run=dry_run,
        subprocess_run=subprocess_run,
        now=current_time,
    )
    if next_offset is not None and not dry_run:
        offsets = state.setdefault("event_offsets", {})
        if isinstance(offsets, dict):
            offsets[DAEMON_EVENT_SINK_PATH] = next_offset
            _save_state(project_root, state)
    return {
        "event_count": len(normalized_events),
        "job_count": len(jobs),
        "events": normalized_events,
        "jobs": jobs,
        "results": results,
    }


def run_project_agent_jobs(
    project: Mapping[str, Any],
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    subprocess_run: SubprocessRun = subprocess.run,
    config: AgentsConfig | None = None,
) -> dict[str, Any]:
    """Run one daemon tick for scheduled and event agents on a project."""

    resolved_config = config or load_project_config(project)
    if resolved_config is None:
        return {
            "scheduled": {"job_count": 0, "jobs": [], "results": []},
            "events": {"event_count": 0, "job_count": 0, "events": [], "jobs": [], "results": []},
        }
    current_time = now or _now()
    scheduled = run_scheduled_agent_jobs(
        project,
        resolved_config,
        now=current_time,
        dry_run=dry_run,
        subprocess_run=subprocess_run,
    )
    events = run_event_agent_jobs(
        project,
        resolved_config,
        now=current_time,
        dry_run=dry_run,
        subprocess_run=subprocess_run,
    )
    return {"scheduled": scheduled, "events": events}
