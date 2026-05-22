"""Pure payload-building helpers for the Web UI.

Imported and re-exported by :mod:`codepilot.webui` so historical attribute
access (``webui.dashboard_payload``, ``webui.task_detail_payload``) keeps
working. These functions never mutate the shared UI state; they read from the
DB and compose JSON-shaped dicts.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

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
    except Exception:  # noqa: BLE001
        # 读取 ai_usage 状态失败时使用已收集的数据
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
    except Exception:  # noqa: BLE001
        # 读取 ai_provider 状态失败时使用已收集的数据
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
            provider = resolve_api_provider(key, provider_ref) if provider_ref else API_PROVIDERS[key]
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
    is fresh. A stale heartbeat with a live PID is treated as stale metadata
    and cleared, which handles PID reuse after an older daemon died.
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
        except (ValueError, TypeError):
            # pid 无法转换为整数时默认为 0
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
                try:
                    db.clear_service_state("daemon", str(state.get("scope") or out["project"] or ""))
                except Exception:  # noqa: BLE001
                    pass
                return _base_payload()
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
    except Exception:  # noqa: BLE001
        # 获取 daemon 状态失败时使用默认状态
        daemon_status = {"running": False, "pid": 0, "project": project["name"], "log": ""}
    try:
        from codepilot.commands.inspect import inspect_service_status
        inspect_status = inspect_service_status(project["name"])
    except Exception:  # noqa: BLE001
        # 获取 inspect 状态失败时使用默认状态
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
        "workflow": project_workflow_payload(project["name"]),
    }


def project_workflow_payload(project: str) -> dict:
    """Return the latest workflow state plus inspect context for a project."""
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    try:
        from codepilot.commands.inspect_workflow import read_inspect_workflow_context
        from codepilot.commands.workflow import workflow_next_payload, workflow_status_payload

        inspect_context = read_inspect_workflow_context(project_info) or {}
        next_payload = workflow_next_payload(project)
    except Exception:  # noqa: BLE001
        inspect_context = {}
        next_payload = {"next_actions": []}
    try:
        status_payload = workflow_status_payload(project)
    except Exception:  # noqa: BLE001
        status_payload = {"state": None, "agent_session": None}
    return {
        "status": status_payload,
        "next_actions": next_payload.get("next_actions") or [],
        "inspect": {
            "context_path": str(inspect_context.get("context_path") or ""),
            "quality_summary": inspect_context.get("quality_summary") or {},
            "created_preview": inspect_context.get("created_preview") or [],
            "report_only": inspect_context.get("report_only") or [],
            "dropped": inspect_context.get("dropped") or [],
            "skipped": inspect_context.get("skipped") or [],
            "source_command": str(inspect_context.get("source_command") or ""),
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
        except Exception:  # noqa: BLE001
            # 获取 jobs 失败时使用空列表
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


def _artifact_type_from_context(ctx: dict[str, Any]) -> str:
    state = ctx.get("state") if isinstance(ctx.get("state"), dict) else {}
    return str(state.get("mode") or ctx.get("artifact_type") or "").strip().lower()


def _artifact_paths_from_context(ctx: dict[str, Any]) -> dict[str, str]:
    paths: dict[str, str] = {}
    state = ctx.get("state") if isinstance(ctx.get("state"), dict) else {}
    for payload in (state, ctx):
        artifacts = payload.get("artifact_paths") if isinstance(payload, dict) else None
        if isinstance(artifacts, dict):
            for key, value in artifacts.items():
                text = str(value or "").strip()
                if text:
                    paths[str(key)] = text
    return paths


def _artifact_value(ctx: dict[str, Any], key: str) -> str:
    for payload in (ctx, ctx.get("state") if isinstance(ctx.get("state"), dict) else {}):
        if key == "spec" and isinstance(payload, dict) and payload.get("artifact_path"):
            return str(payload.get("artifact_path") or "")
        if key == "task_batch" and isinstance(payload, dict) and payload.get("task_batch_path"):
            return str(payload.get("task_batch_path") or "")
        artifacts = payload.get("artifact_paths") if isinstance(payload, dict) else None
        if isinstance(artifacts, dict) and artifacts.get(key):
            return str(artifacts.get(key) or "")
    return ""


def _command_arg(raw_path: str, placeholder: str) -> str:
    text = str(raw_path or "").strip() or placeholder
    if any(ch.isspace() for ch in text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _artifact_project_info(context_path: Path) -> dict | None:
    try:
        return db.find_project_by_path(context_path)
    except Exception:  # noqa: BLE001
        return None


def _artifact_web_action(project: str, context_path: str, action_id: str) -> dict:
    return {
        "type": "artifact_next_action",
        "method": "POST",
        "endpoint": "/api/artifacts/actions",
        "payload": {
            "project": project,
            "context_path": context_path,
            "action_id": action_id,
        },
    }


def _raw_artifact_next_actions(ctx: dict[str, Any], artifact_type: str) -> list:
    raw_actions = ctx.get("next_actions")
    if isinstance(raw_actions, list) and raw_actions:
        return raw_actions
    try:
        from codepilot.webapp.action_requirements import artifact_next_actions_for_type

        return artifact_next_actions_for_type(artifact_type)
    except Exception:  # noqa: BLE001
        return []


def _materialize_artifact_next_actions(
    ctx: dict[str, Any],
    *,
    context_path: Path,
    artifact_type: str,
    project_info: dict | None,
) -> list[dict]:
    project_name = str((project_info or {}).get("name") or "").strip()
    context_path_text = str(context_path)
    spec_path = _artifact_value(ctx, "spec")
    plan_path = _artifact_value(ctx, "plan")
    task_batch_path = _artifact_value(ctx, "task_batch")
    summary = str(ctx.get("summary") or "")

    actions: list[dict] = []
    for item in _raw_artifact_next_actions(ctx, artifact_type):
        if isinstance(item, dict):
            action = dict(item)
            action_id = str(action.get("id") or "").strip()
        elif isinstance(item, str):
            action_id = item.strip()
            action = {"id": action_id, "label": action_id, "risk": "unknown", "suggested_command": ""}
        else:
            continue
        if not action_id:
            continue
        action["id"] = action_id
        action["label"] = str(action.get("label") or action_id)
        action["risk"] = str(action.get("risk") or "unknown").lower()
        action.setdefault("suggested_command", "")
        action["executable"] = False
        action["params"] = {
            "project": project_name,
            "context_path": context_path_text,
            "artifact_type": artifact_type,
        }

        if action_id == "plan_from_spec" and spec_path:
            action["suggested_command"] = (
                f"codepilot plan -p {project_name or '<project>'} "
                f"--from-spec {_command_arg(spec_path, '<spec_path>')} --json"
            )
            action["artifact_path"] = spec_path
            action["params"]["artifact_path"] = spec_path
            action["executable"] = bool(project_name)
        elif action_id == "import_tasks" and task_batch_path:
            action["suggested_command"] = (
                f"codepilot add -p {project_name or '<project>'} "
                f"-f {_command_arg(task_batch_path, '<task_batch_path>')}"
            )
            action["task_batch_path"] = task_batch_path
            action["params"]["task_batch_path"] = task_batch_path
            action["executable"] = bool(project_name)
        elif action_id == "continue_clarify":
            action["params"]["summary"] = summary
        elif action_id == "abandon_plan" and plan_path:
            action["params"]["plan_path"] = plan_path

        if action["executable"]:
            action["action"] = _artifact_web_action(project_name, context_path_text, action_id)
        actions.append(action)
    return actions


def artifact_context_payload(context_path: str) -> dict:
    """Read an artifact's context JSON and return its content with next_actions.

    Used by the Web UI to display artifact metadata and available next actions.
    Returns an error dict if the context file cannot be read.
    """
    path = Path(context_path)
    if not path.exists():
        return {"ok": False, "error": "artifact context not found"}
    try:
        raw = path.read_text(encoding="utf-8")
        ctx = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "artifact context is corrupt or unreadable"}
    if not isinstance(ctx, dict):
        return {"ok": False, "error": "artifact context is not a JSON object"}
    artifact_type = _artifact_type_from_context(ctx)
    project_info = _artifact_project_info(path.resolve())
    artifact_paths = _artifact_paths_from_context(ctx)
    spec_path = _artifact_value(ctx, "spec")
    plan_path = _artifact_value(ctx, "plan")
    task_batch_path = _artifact_value(ctx, "task_batch")
    artifact_path = spec_path if artifact_type == "clarify" else plan_path
    return {
        "ok": True,
        "summary": str(ctx.get("summary") or ""),
        "next_actions": _materialize_artifact_next_actions(
            ctx,
            context_path=path.resolve(),
            artifact_type=artifact_type,
            project_info=project_info,
        ),
        "artifact_type": artifact_type,
        "context_path": str(path.resolve()),
        "artifact_path": artifact_path,
        "plan_path": plan_path,
        "task_batch_path": task_batch_path,
        "artifact_paths": artifact_paths,
    }
