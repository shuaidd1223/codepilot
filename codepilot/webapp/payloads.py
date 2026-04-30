"""Pure payload-building helpers for the Web UI.

Imported and re-exported by :mod:`codepilot.webui` so historical attribute
access (``webui.dashboard_payload``, ``webui.task_detail_payload``) keeps
working. These functions never mutate the shared UI state; they read from the
DB and compose JSON-shaped dicts.
"""

from __future__ import annotations

import sys
from datetime import datetime

from codepilot.storage import database as db
from codepilot.webapp.display_sort import TASK_STATUS_ORDER, sort_tasks_for_display
from codepilot.core.runtime import runtime_summary
from codepilot.core.config import resolve_project_config_reference
from codepilot.webapp.live_output_payloads import (
    normalize_legacy_live_output_markdown as _normalize_legacy_live_output_markdown,
)
from codepilot.webapp.task_payloads import (
    _compose_log_text,
    _parse_depends,
    _read_text,
    _tail_text,
    _task_payload,
    task_detail_payload,
    task_log_delta,
)


STATUS_ORDER = TASK_STATUS_ORDER


def _shell():
    """Return the ``codepilot.webui`` shell module for dynamic lookups."""
    return sys.modules["codepilot.webapp.server"]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _usage_stats_from_service_state() -> dict:
    out: dict[str, dict] = {}
    try:
        for state in db.list_service_states("ai_usage"):
            meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
            provider = str(meta.get("provider") or state.get("scope") or "").strip()
            if not provider:
                continue
            out[provider] = {
                "provider": provider,
                "name": str(meta.get("name") or provider),
                "requests": int(meta.get("requests") or 0),
                "prompt_tokens": int(meta.get("prompt_tokens") or 0),
                "completion_tokens": int(meta.get("completion_tokens") or 0),
                "total_tokens": int(meta.get("total_tokens") or 0),
                "reasoning_tokens": int(meta.get("reasoning_tokens") or 0),
                "prompt_cache_hit_tokens": int(meta.get("prompt_cache_hit_tokens") or 0),
                "prompt_cache_miss_tokens": int(meta.get("prompt_cache_miss_tokens") or 0),
                "updated_at": str(meta.get("updated_at") or state.get("updated_at") or ""),
                "by_model": meta.get("by_model") if isinstance(meta.get("by_model"), dict) else {},
            }
    except Exception:
        return out
    return out


def _provider_availability_from_service_state() -> dict:
    out: dict[str, dict] = {}
    try:
        for state in db.list_service_states("ai_provider"):
            scope = str(state.get("scope") or "").strip()
            meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
            provider = str(meta.get("provider") or scope).strip()
            if not provider:
                continue
            out[provider] = {
                "status": str(state.get("status") or ""),
                "reason": str(meta.get("reason") or ""),
                "source": str(meta.get("source") or ""),
                "updated_at": str(meta.get("updated_at") or state.get("updated_at") or ""),
            }
    except Exception:
        return out
    return out


def ai_status_payload(project: str | None = None, *, refresh_balance: bool = False) -> dict:
    """Return AI provider balance and local token usage stats for Web UI."""
    db.init_db()
    project_info = db.get_project(project) if project else None
    provider_ref = resolve_project_config_reference(project_info) if project_info else None
    providers: dict[str, dict] = {}
    balances: dict[str, dict] = {}
    availability = _provider_availability_from_service_state()

    try:
        from codepilot.ai_support.providers import API_PROVIDERS, fetch_provider_balance, resolve_api_provider

        for key in sorted(API_PROVIDERS):
            provider = resolve_api_provider(key, provider_ref)
            configured = bool(provider.resolve_api_key() or not provider.requires_api_key())
            providers[key] = {
                "name": provider.name,
                "type": provider.provider_type,
                "model": provider.model,
                "base_url": provider.base_url,
                "configured": configured,
                "has_balance": bool(getattr(provider, "balance_endpoint", "")),
                "availability": availability.get(key, {}),
            }
            if key == "deepseek" and configured and refresh_balance:
                try:
                    balances[key] = fetch_provider_balance(provider)
                except Exception as exc:  # noqa: BLE001
                    balances[key] = {"available": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "providers": providers,
            "balances": balances,
            "usage": _usage_stats_from_service_state(),
            "availability": availability,
        }

    return {
        "ok": True,
        "providers": providers,
        "balances": balances,
        "usage": _usage_stats_from_service_state(),
        "availability": availability,
    }
def daemon_health_payload(project: str | None = None, *, stale_after_seconds: int = 120) -> dict:
    """Introspect daemon liveness from ``service_states``.

    Used by the Web UI to surface a banner when the daemon appears dead —
    tasks would otherwise silently sit in ``backlog`` forever with no visible
    hint that nothing is draining the queue.

    Returns ``{alive, running, pid, last_heartbeat, stale_seconds, reason}``:
    ``alive`` is True iff the daemon process is running AND its heartbeat
    is fresh. ``running`` means only the PID check, so we can distinguish
    "stopped" from "frozen".
    """
    from codepilot.core.runtime import is_process_alive

    def _base_payload() -> dict:
        return {
            "alive": False,
            "running": False,
            "pid": 0,
            "project": "",
            "last_heartbeat": "",
            "stale_seconds": 0,
            "stale_after_seconds": int(stale_after_seconds),
            "reason": "daemon 未运行",
        }

    def _payload_from_state(state: dict | None) -> dict:
        out = _base_payload()
        if not state:
            return out
        meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
        out["project"] = str(meta.get("project") or state.get("scope") or "")
        try:
            db_pid = int(state.get("pid") or 0)
        except Exception:
            db_pid = 0
        if db_pid:
            out["pid"] = db_pid
            out["running"] = bool(is_process_alive(db_pid))
        out["last_heartbeat"] = str(state.get("heartbeat_at") or "")
        if out["running"] and out["last_heartbeat"]:
            try:
                delta = (datetime.now() - datetime.fromisoformat(out["last_heartbeat"])).total_seconds()
            except ValueError:
                delta = -1
            if delta >= 0:
                out["stale_seconds"] = int(max(0, delta))
                if delta <= stale_after_seconds:
                    out["alive"] = True
                    out["reason"] = ""
                    return out
                out["reason"] = f"daemon 心跳 {int(delta)}s 未更新（>{stale_after_seconds}s 阈值），可能已假死"
                return out
        return out

    out = _base_payload()
    scope = (project or "").strip()
    if scope:
        state = db.get_service_state("daemon", scope)
        return _payload_from_state(state)

    candidates = []
    for state in db.list_service_states("daemon"):
        payload = _payload_from_state(state)
        payload["_updated_at"] = str(state.get("updated_at") or state.get("heartbeat_at") or "")
        candidates.append(payload)

    if not candidates:
        return out

    def _rank(item: dict) -> tuple[int, str]:
        if item.get("alive"):
            return (0, str(item.get("_updated_at") or ""))
        if item.get("running"):
            return (1, str(item.get("_updated_at") or ""))
        if item.get("last_heartbeat"):
            return (2, str(item.get("_updated_at") or ""))
        return (3, str(item.get("_updated_at") or ""))

    best = max(candidates, key=lambda item: (-_rank(item)[0], _rank(item)[1]))
    best.pop("_updated_at", None)
    if not best.get("alive") and best.get("project") and best.get("reason"):
        best["reason"] = f"项目 {best['project']}: {best['reason']}"
    return best


def _sorted_tasks(tasks: list[dict]) -> list[dict]:
    return sort_tasks_for_display(tasks)


def project_summary(project: dict, *, job_count: int | None = None) -> dict:
    """Summary tile for the sidebar. ``job_count`` is optional because jobs
    live in in-memory shell state (``_UI_JOBS``) — the dashboard entry point
    injects it so we don't pull the shell import from every call site."""
    stats = db.get_task_stats(project["name"])
    stats = {
        **stats,
        "total": sum(
            int(stats.get(key) or 0)
            for key in ("backlog", "in_progress", "done", "failed", "cancelled")
        ),
    }
    tasks = _sorted_tasks(db.list_tasks(project=project["name"]))
    live = next((task for task in tasks if task["status"] == "in_progress"), None)
    session_count = len(db.list_sessions(project=project["name"]))
    try:
        from codepilot.commands.daemon import daemon_service_status
        daemon_status = daemon_service_status(project["name"])
    except Exception:
        daemon_status = {"running": False, "pid": 0, "project": project["name"], "log": ""}
    try:
        from codepilot.commands.inspect import inspect_service_status
        inspect_status = inspect_service_status(project["name"])
    except Exception:
        inspect_status = {"running": False, "pid": 0, "project": project["name"], "log": ""}
    return {
        "name": project["name"],
        "path": project["path"],
        "stats": stats,
        "session_count": session_count,
        "job_count": int(job_count or 0),
        "active_summary": runtime_summary(live) if live else "",
        "services": {
            "tasks": daemon_status,
            "inspect": inspect_status,
        },
    }


def dashboard_payload(selected_project: str | None = None) -> dict:
    """Snapshot of the full workspace for the sidebar + current-project view.

    Returns tasks/jobs **for every known project** (keyed by name) so the
    frontend can hydrate its per-project cache once and then make project
    switching a pure navigation update — no extra round-trip, no
    "wrong-project tasks briefly show up" race.

    ``tasks`` / ``jobs`` (non-plural-keyed) are retained as a convenience
    alias of the currently-selected project's slice so existing callers
    and tests don't break.
    """
    shell = _shell()
    db.init_db()
    project_rows = db.list_projects()

    tasks_by_project: dict[str, list[dict]] = {}
    jobs_by_project: dict[str, list[dict]] = {}
    for proj in project_rows:
        name = proj["name"]
        raw_tasks = [
            task
            for task in _sorted_tasks(db.list_tasks(project=name))
            if str(task.get("status") or "") != "archived"
        ]
        tasks_by_project[name] = [_task_payload(task) for task in raw_tasks]
        try:
            jobs_by_project[name] = shell.list_ui_jobs(name)
        except Exception:
            jobs_by_project[name] = []

    projects = [
        project_summary(proj, job_count=len(jobs_by_project.get(proj["name"], [])))
        for proj in project_rows
    ]
    resolved = selected_project or (projects[0]["name"] if projects else None)
    return {
        "projects": projects,
        "selected_project": resolved,
        "tasks_by_project": tasks_by_project,
        "jobs_by_project": jobs_by_project,
        # Back-compat aliases for the currently-selected project.
        "tasks": tasks_by_project.get(resolved, []) if resolved else [],
        "jobs": jobs_by_project.get(resolved, []) if resolved else [],
        "events": shell.list_ui_events(resolved),
    }
