"""Pure payload-building helpers for the Web UI.

Imported and re-exported by :mod:`codepilot.webui` so historical attribute
access (``webui.dashboard_payload``, ``webui.task_detail_payload``) keeps
working. These functions never mutate the shared UI state; they read from the
DB and compose JSON-shaped dicts.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from codepilot.storage import database as db
from codepilot.commands.reviewer_output import parse_reviewer_output
from codepilot.webapp.display_sort import TASK_STATUS_ORDER, sort_tasks_for_display
from codepilot.core.runtime import runtime_summary
from codepilot.core.config import resolve_project_config_reference


STATUS_ORDER = TASK_STATUS_ORDER
_LIVE_HEAD_RE = re.compile(r"^\s*##\s+Live Output\s*$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_-]+)?\s*$")
_ROLE_MARK_RE = re.compile(r"^(user|codex|claude|assistant)$", re.IGNORECASE)


def _shell():
    """Return the ``codepilot.webui`` shell module for dynamic lookups."""
    return sys.modules["codepilot.webapp.server"]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tail_text(text: str, *, max_lines: int = 160, max_chars: int = 20000) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
    return text[-max_chars:] if len(text) > max_chars else text


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    target = Path(path)
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


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


def _find_live_output_header(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if _LIVE_HEAD_RE.match(str(line or "").strip()):
            return i
    return -1


def _find_non_empty_line(lines: list[str], start_idx: int) -> int:
    idx = max(0, int(start_idx))
    while idx < len(lines) and not str(lines[idx] or "").strip():
        idx += 1
    return idx


def _parse_text_fence_open(line: str) -> str | None:
    open_m = _FENCE_RE.match(str(line or "").strip())
    if not open_m:
        return None
    lang = (open_m.group(2) or "").lower()
    if lang and lang != "text":
        return None
    return open_m.group(1)[0]


def _find_fence_close(lines: list[str], start_idx: int, *, fence_char: str) -> int:
    for i in range(start_idx + 1, len(lines)):
        m = _FENCE_RE.match(str(lines[i] or "").strip())
        if m and m.group(1)[0] == fence_char:
            return i
    return -1


def _legacy_live_marker(line: str) -> tuple[str, str] | None:
    trimmed = str(line or "").strip()
    if _ROLE_MARK_RE.match(trimmed):
        return ("role", trimmed[0].upper() + trimmed[1:].lower())
    if trimmed.lower() == "exec":
        return ("exec", "Exec")
    return None


def _has_legacy_live_markers(lines: list[str]) -> bool:
    return any(_legacy_live_marker(str(line or "")) for line in lines)


def _close_live_runtime_block(out: list[str], state: dict[str, object]) -> None:
    if not state["runtime_open"]:
        return
    out.extend(["~~~", ""])
    state["runtime_open"] = False


def _close_live_exec_block(out: list[str], state: dict[str, object]) -> None:
    if not state["exec_open"]:
        return
    out.extend(["~~~", ""])
    state["exec_open"] = False


def _open_live_role_section(out: list[str], state: dict[str, object], role_name: str) -> None:
    _close_live_runtime_block(out, state)
    _close_live_exec_block(out, state)
    out.extend([f"### {role_name}", ""])
    state["mode"] = role_name.lower()


def _open_live_exec_section(out: list[str], state: dict[str, object]) -> None:
    _close_live_runtime_block(out, state)
    _close_live_exec_block(out, state)
    out.extend(["### Exec", "", "~~~text"])
    state["exec_open"] = True
    state["mode"] = "exec"


def _open_live_runtime_section(out: list[str], state: dict[str, object]) -> None:
    if state["mode"] == "runtime" and state["runtime_open"]:
        return
    _close_live_exec_block(out, state)
    if not state["runtime_open"]:
        out.extend(["### Runtime", "", "~~~text"])
    state["runtime_open"] = True
    state["mode"] = "runtime"


def _normalize_legacy_live_inner_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    state: dict[str, object] = {"mode": "", "runtime_open": False, "exec_open": False}

    for line in lines:
        raw = str(line or "")
        marker = _legacy_live_marker(raw)
        if marker:
            marker_kind, marker_name = marker
            if marker_kind == "role":
                _open_live_role_section(out, state, marker_name)
                continue
            _open_live_exec_section(out, state)
            continue

        if not state["mode"]:
            _open_live_runtime_section(out, state)
        out.append(raw)

    _close_live_runtime_block(out, state)
    _close_live_exec_block(out, state)
    return out


def _normalize_legacy_live_output_markdown(text: str) -> str:
    """Convert legacy ``## Live Output`` fenced text protocol to markdown sections.

    Older runs stored Codex/Claude stream protocol as one giant ``~~~text`` block:
    ``user`` / ``codex`` / ``exec`` markers remained plain text so the renderer
    couldn't style or parse markdown content inside that fence.
    """
    if not text:
        return text

    lines = text.splitlines()
    if not lines:
        return text

    head_idx = _find_live_output_header(lines)
    if head_idx < 0:
        return text

    body_start = _find_non_empty_line(lines, head_idx + 1)
    if body_start >= len(lines):
        return text

    fence_char = _parse_text_fence_open(str(lines[body_start] or ""))
    if not fence_char:
        return text

    body_end = _find_fence_close(lines, body_start, fence_char=fence_char)
    has_closing_fence = body_end >= 0
    if not has_closing_fence:
        body_end = len(lines)

    inner = lines[body_start + 1 : body_end]
    if not _has_legacy_live_markers(inner):
        return text

    out = lines[: head_idx + 1] + [""]
    out.extend(_normalize_legacy_live_inner_lines(inner))

    tail_start = body_end + 1 if has_closing_fence else body_end
    out.extend(lines[tail_start:])
    normalized = "\n".join(out).rstrip() + "\n"
    return normalized


def _parse_depends(raw: str | None) -> list[int]:
    if not raw:
        return []
    parsed: object = raw
    # Historical DB rows may accidentally hold a double-encoded dependency
    # payload (e.g. "\"[]\""). Decode string payloads iteratively so those
    # rows still render in the detail API instead of raising ValueError.
    for _ in range(3):
        if not isinstance(parsed, str):
            break
        text = parsed.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Backward-compatible fallback for legacy comma-separated inputs.
            parsed = [part.strip() for part in text.split(",") if part.strip()]
            break

    if parsed is None:
        return []
    if not isinstance(parsed, (list, tuple, set)):
        parsed = [parsed]

    out: list[int] = []
    seen: set[int] = set()
    for item in parsed:
        try:
            dep = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if dep <= 0 or dep in seen:
            continue
        seen.add(dep)
        out.append(dep)
    return out


# Cap per-request log delta at 2 MiB so a one-shot `/log?offset=0` on a huge
# file doesn't block the event loop or fill the client buffer. The frontend
# loops on `next_offset` until `done` to page in the rest.
_LOG_CHUNK_MAX_BYTES = 2 * 1024 * 1024


def task_log_delta(task_id: int, *, offset: int = 0) -> dict:
    """Return a `{offset, next_offset, size, text, done, path}` slice of the
    task's current log file starting at *offset* bytes.

    Used by the Web UI to stream the full log incrementally instead of
    re-tailing on every poll — the frontend keeps a running buffer, asks for
    `?offset=<bytes_consumed>` on each update, and appends the returned
    `text` until `done=True`.
    """
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")

    path_str = task.get("current_log_path") or ""
    out: dict = {
        "task_id": task_id,
        "path": path_str,
        "offset": max(0, int(offset or 0)),
        "next_offset": max(0, int(offset or 0)),
        "size": 0,
        "text": "",
        "done": True,
    }

    if not path_str:
        return out

    target = Path(path_str)
    if not target.exists():
        return out

    # Legacy runs may still contain raw protocol under one ``~~~text`` fence
    # (no markdown sections inside Live Output). For completed tasks we can
    # safely normalize in-place so both API polling and direct file viewing
    # become markdown-native.
    try:
        if str(task.get("status") or "") != "in_progress":
            raw_full = target.read_text(encoding="utf-8", errors="replace")
            normalized = _normalize_legacy_live_output_markdown(raw_full)
            if normalized != raw_full:
                target.write_text(normalized, encoding="utf-8")
    except Exception:
        pass

    try:
        size = target.stat().st_size
    except OSError:
        return out

    out["size"] = size
    start = out["offset"]
    if start >= size:
        out["next_offset"] = size
        out["done"] = True
        return out

    # Read only the delta so large tails stay fast. Binary-safe open + decode
    # with replace to tolerate partial multibyte writes.
    end = min(size, start + _LOG_CHUNK_MAX_BYTES)
    try:
        with target.open("rb") as fh:
            fh.seek(start)
            raw = fh.read(end - start)
    except OSError:
        return out

    text = raw.decode("utf-8", errors="replace")
    out["text"] = text
    out["next_offset"] = end
    out["done"] = end >= size
    return out


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


def _compose_log_text(task: dict) -> str:
    live = _read_text(task.get("current_log_path"))
    if live:
        return _tail_text(live)

    logs = db.list_task_logs(task["id"])
    if logs:
        blocks = []
        for entry in logs:
            header = f"[{entry.get('phase') or '-'}] agent={entry.get('agent') or '-'} exit={entry.get('exit_code') if entry.get('exit_code') is not None else '-'}"
            blocks.append(header)
            if entry.get("output"):
                blocks.append(entry["output"].strip())
        return _tail_text("\n\n".join(blocks))

    return _tail_text(task.get("last_output") or task.get("error_message") or task.get("delivery_record") or "")


def _task_payload(task: dict) -> dict:
    status = task["status"]

    # Best-effort ETA for this task based on historical median duration of
    # tasks run by the same agent in the same project. Only surfaces for
    # pending / in-progress tasks — done tasks show actual runtime instead.
    eta_seconds: int | None = None
    if status in {"backlog", "in_progress"}:
        try:
            eta_seconds = db.compute_agent_eta_seconds(task["project"], task.get("agent") or None)
        except Exception:
            eta_seconds = None

    # Preflight skip re-queues the task to backlog and stores the reason in
    # ``error_message``. That isn't a real failure — it's a "postponed, fix
    # this thing and I'll retry" warning. Surface it as ``skip_reason`` so
    # the UI can render it as a neutral / warning block instead of red.
    raw_error = task.get("error_message") or ""
    skip_reason = ""
    error_message = ""
    if status == "backlog" and raw_error:
        skip_reason = raw_error
    else:
        error_message = raw_error

    return {
        "id": task["id"],
        "project": task["project"],
        "title": task["title"],
        "status": status,
        "priority": task["priority"],
        "agent": task["agent"],
        "source": task.get("source") or "user",
        "phase": task.get("run_phase") or "",
        "runtime": runtime_summary(task) if status == "in_progress" else "",
        "eta_seconds": eta_seconds,
        "latest": task.get("last_output") or skip_reason or error_message or task.get("delivery_record") or "",
        "error_message": error_message,
        "skip_reason": skip_reason,
        "delivery_record": task.get("delivery_record") or "",
        "created_at": task.get("created_at") or "",
        "started_at": task.get("started_at") or "",
        "completed_at": task.get("completed_at") or "",
        "retry_count": int(task.get("retry_count") or 0),
        "max_retries": int(task.get("max_retries") or 0),
        "actions": {
            "retry": status in {"failed", "cancelled", "backlog"},
            "stop": status == "in_progress",
            "promote": status in {"backlog", "failed", "cancelled"},
            "cancel": status in {"backlog", "failed"},
            "archive": status == "done",
            "delete": status in {"backlog", "cancelled", "done", "archived"},
        },
    }


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


def _review_block_for_log(entry: dict) -> dict | None:
    """If ``entry`` is a reviewer phase log, return its parsed verdict block.

    Reviewer phases are detected by ``phase`` or ``agent`` containing the
    substring ``review`` (matches builder=codex / agent="codex-review" /
    phase="reviewer r2" etc.). Non-reviewer entries or empty transcripts
    return ``None``, so the frontend can treat ``review is null`` as
    "no structured verdict to show for this entry".
    """
    phase = str(entry.get("phase") or "").lower()
    agent = str(entry.get("agent") or "").lower()
    if "review" not in phase and "review" not in agent:
        return None
    raw = entry.get("output") or ""
    if not raw.strip():
        return None
    parsed = parse_reviewer_output(raw)
    if parsed.source == "empty":
        return None
    return {
        "verdict": parsed.verdict,
        "source": parsed.source,
        "ac_checks": parsed.ac_checks,
        "blockers": parsed.blockers,
        "advisory": parsed.advisory,
    }


def task_detail_payload(task_id: int) -> dict:
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    payload = _task_payload(task)
    log_entries = list(db.list_task_logs(task_id))
    logs: list[dict] = []
    latest_review: dict | None = None
    for entry in log_entries:
        review_block = _review_block_for_log(entry)
        logs.append(
            {
                "phase": entry.get("phase") or "",
                "agent": entry.get("agent") or "",
                "exit_code": entry.get("exit_code"),
                "output_excerpt": _tail_text(entry.get("output") or "", max_lines=40, max_chars=5000),
                "review": review_block,
            }
        )
        if review_block is not None:
            latest_review = {
                **review_block,
                "phase": entry.get("phase") or "",
                "agent": entry.get("agent") or "",
            }
    payload.update(
        {
            "content": task.get("content") or "",
            "depends_on": _parse_depends(task.get("depends_on")),
            "project_path": task.get("project_path") or "",
            "current_log_path": task.get("current_log_path") or "",
            "log_text": _compose_log_text(task),
            "logs": logs,
            "latest_review": latest_review,
        }
    )
    return payload

