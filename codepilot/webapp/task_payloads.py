"""Task-oriented payload builders for the Web UI."""

from __future__ import annotations

import json
import re
from pathlib import Path

from codepilot.commands.reviewer_output import parse_reviewer_output
from codepilot.core.paths import global_storage_root, project_storage_root
from codepilot.core.runtime import runtime_summary
from codepilot.core.task_template import missing_task_template_sections
from codepilot.core.work_item import task_work_item_payload
from codepilot.core.workflow_state import read_task_execution_artifacts, read_task_timeline_events
from codepilot.storage import database as db


WORKFLOW_BOARD_COLUMNS = (
    {
        "id": "backlog",
        "title": "Backlog",
        "description": "已创建但缺少可执行任务模板或验收信息。",
        "tone": "neutral",
    },
    {
        "id": "ready",
        "title": "Ready",
        "description": "验收标准和验证命令齐全，可由执行器领取。",
        "tone": "primary",
    },
    {
        "id": "running",
        "title": "Running",
        "description": "正在由 daemon 或手动 run 执行。",
        "tone": "info",
    },
    {
        "id": "review",
        "title": "Review",
        "description": "正在审查阶段，需要查看日志或 reviewer verdict。",
        "tone": "warning",
    },
    {
        "id": "blocked",
        "title": "Blocked",
        "description": "缺配置、执行失败、测试失败或等待人工处理。",
        "tone": "danger",
    },
    {
        "id": "done",
        "title": "Done",
        "description": "已完成并通过验证。",
        "tone": "success",
    },
)

_WORKFLOW_COLUMN_IDS = tuple(column["id"] for column in WORKFLOW_BOARD_COLUMNS)


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
_PHASE_LOG_KEY_RE = re.compile(r"[^a-z0-9_.-]+")
_PHASE_ROUND_RE = re.compile(r"(?:^|[-_\s])r(?:ound)?[-_\s]*(\d+)\b|round[-_\s]*(\d+)\b", re.IGNORECASE)


def task_log_delta(task_id: int, *, offset: int = 0) -> dict:
    """Return a `{offset, next_offset, size, text, done, path}` slice of the
    task's current log file starting at *offset* bytes.

    Used by the Web UI to stream the full log incrementally instead of
    re-tailing on every poll - the frontend keeps a running buffer, asks for
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


def _safe_phase_key(value: str) -> str:
    key = _PHASE_LOG_KEY_RE.sub("-", str(value or "").strip().lower()).strip(".-")
    return key or "phase"


def _phase_kind(phase: str, agent: str = "") -> str:
    text = f"{phase} {agent}".lower()
    if "builder" in text:
        return "builder"
    if "review" in text:
        return "reviewer"
    if "preflight" in text:
        return "preflight"
    if "merge" in text:
        return "merge"
    return _safe_phase_key(phase or agent or "phase")


def _phase_round(phase: str) -> int | None:
    text = str(phase or "")
    match = _PHASE_ROUND_RE.search(text)
    if not match:
        return None
    for group in match.groups():
        if not group:
            continue
        try:
            value = int(group)
        except ValueError:
            continue
        return value if value > 0 else None
    return None


def _phase_label(kind: str, phase: str, round_num: int | None = None) -> str:
    labels = {
        "builder": "Builder",
        "reviewer": "Reviewer",
        "preflight": "Preflight",
        "merge": "Merge",
    }
    base = labels.get(kind) or " ".join(part.capitalize() for part in _safe_phase_key(phase or kind).split("-"))
    if kind in {"builder", "reviewer"} and round_num:
        return f"{base} R{round_num}"
    return base or "Phase"


def _phase_status(entry: dict, task: dict, review_block: dict | None, *, active: bool = False) -> str:
    if active:
        return "running"
    verdict = str((review_block or {}).get("verdict") or "").lower()
    if verdict == "fail":
        return "failed"
    exit_code = entry.get("exit_code")
    if exit_code is not None:
        try:
            return "done" if int(exit_code) == 0 else "failed"
        except (TypeError, ValueError):
            return "unknown"
    if entry.get("finished_at"):
        return "done"
    if str(task.get("status") or "") == "failed":
        return "failed"
    return "unknown"


def _phase_summary(entry: dict, status: str, review_block: dict | None) -> str:
    phase = str(entry.get("phase") or "phase").strip() or "phase"
    exit_code = entry.get("exit_code")
    duration = entry.get("duration")
    parts = [f"{phase} 阶段"]
    if status == "running":
        parts.append("运行中")
    elif status == "failed":
        parts.append("需要关注")
    elif status == "done":
        parts.append("已完成")
    if exit_code is not None:
        parts.append(f"exit={exit_code}")
    if duration not in (None, ""):
        parts.append(f"{duration}s")
    verdict = str((review_block or {}).get("verdict") or "").strip()
    if verdict:
        parts.append(f"review={verdict.upper()}")
    return " · ".join(parts)


def _phase_default_expanded(status: str, *, active: bool = False) -> bool:
    return active or status in {"failed", "running", "unknown"}


def _path_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_phase_log_path(task: dict, raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    project_path = str(task.get("project_path") or "").strip()
    raw_target = Path(raw_path).expanduser()
    if not raw_target.is_absolute() and project_path:
        raw_target = Path(project_path).expanduser() / raw_target
    try:
        target = raw_target.resolve(strict=False)
    except OSError:
        return None
    task_id = int(task.get("id") or 0)
    name = target.name.lower()
    if task_id <= 0 or not name.startswith(f"task-{task_id}-") or not name.endswith(".console.md"):
        return None
    try:
        if not target.is_file():
            return None
    except OSError:
        return None

    roots: list[Path] = []
    if project_path:
        try:
            roots.append(Path(project_path).expanduser().resolve(strict=False))
        except OSError:
            pass
    try:
        roots.append(
            project_storage_root(
                project_name=str(task.get("project") or ""),
                project_path=project_path or None,
            ).resolve(strict=False)
        )
    except OSError:
        pass
    try:
        roots.append(global_storage_root().resolve(strict=False))
    except OSError:
        pass

    return target if any(_path_under(target, root) for root in roots) else None


def _timeline_phase_path(entry: dict, timeline: list[dict], used: set[int]) -> str:
    started = str(entry.get("started_at") or "")
    phase = str(entry.get("phase") or "").strip().lower()

    if started:
        for idx, item in enumerate(timeline):
            if idx in used:
                continue
            if str(item.get("source") or "") != "codepilot.task_log":
                continue
            if str(item.get("event") or "") != "agent_started":
                continue
            if str(item.get("time") or "") != started:
                continue
            path = str(item.get("artifact_path") or "").strip()
            if path:
                used.add(idx)
                return path

    for idx, item in enumerate(timeline):
        if idx in used:
            continue
        if str(item.get("event") or "") != "agent_started":
            continue
        path = str(item.get("artifact_path") or "").strip()
        message = str(item.get("message") or "").lower()
        if path and phase and phase in message:
            used.add(idx)
            return path
    return ""


def _phase_raw_source(task: dict, raw_path: str, output: str) -> tuple[str, Path | None, int, str]:
    safe_path = _safe_phase_log_path(task, raw_path)
    if safe_path is not None:
        try:
            return "file", safe_path, safe_path.stat().st_size, safe_path.name
        except OSError:
            pass
    if output:
        return "db", None, len(output.encode("utf-8", errors="replace")), ""
    return "none", None, 0, ""


def _phase_entry_payload(
    *,
    task: dict,
    entry: dict,
    index: int,
    kind: str,
    round_num: int | None,
    raw_path: str = "",
    active: bool = False,
    include_private: bool = False,
) -> dict:
    output = str(entry.get("output") or "")
    review_block = _review_block_for_log(entry)
    status = _phase_status(entry, task, review_block, active=active)
    raw_source, safe_path, raw_size, raw_filename = _phase_raw_source(task, raw_path, output)
    phase = str(entry.get("phase") or kind or "phase")
    key = f"phase-{index}-{_safe_phase_key(kind)}"
    if round_num:
        key += f"-r{round_num}"
    payload = {
        "key": key,
        "index": index,
        "kind": kind,
        "phase": phase,
        "label": _phase_label(kind, phase, round_num),
        "round": round_num,
        "agent": str(entry.get("agent") or ""),
        "status": status,
        "active": bool(active),
        "exit_code": entry.get("exit_code"),
        "started_at": entry.get("started_at") or "",
        "finished_at": entry.get("finished_at") or "",
        "duration": entry.get("duration"),
        "summary": _phase_summary(entry, status, review_block),
        "output_excerpt": _tail_text(output, max_lines=12, max_chars=2000),
        "raw_available": raw_source != "none",
        "raw_source": raw_source,
        "raw_size": raw_size,
        "raw_filename": raw_filename,
        "default_expanded": _phase_default_expanded(status, active=active),
        "review": review_block,
    }
    if include_private:
        payload["_raw_path"] = str(safe_path or "")
        payload["_output"] = output
    return payload


def _task_phase_logs(task: dict, log_entries: list[dict], timeline: list[dict], *, include_private: bool = False) -> list[dict]:
    phases: list[dict] = []
    used_timeline: set[int] = set()
    counters: dict[str, int] = {}
    current_log_path = str(task.get("current_log_path") or "").strip()

    for index, entry in enumerate(log_entries):
        phase = str(entry.get("phase") or "")
        kind = _phase_kind(phase, str(entry.get("agent") or ""))
        explicit_round = _phase_round(phase)
        if kind in {"builder", "reviewer"}:
            if explicit_round:
                counters[kind] = max(counters.get(kind, 0), explicit_round)
                round_num = explicit_round
            else:
                counters[kind] = counters.get(kind, 0) + 1
                round_num = counters[kind]
        else:
            round_num = explicit_round

        raw_path = _timeline_phase_path(entry, timeline, used_timeline)
        if not raw_path and index == len(log_entries) - 1:
            raw_path = current_log_path
        phases.append(
            _phase_entry_payload(
                task=task,
                entry=entry,
                index=index,
                kind=kind,
                round_num=round_num,
                raw_path=raw_path,
                include_private=include_private,
            )
        )

    if str(task.get("status") or "") == "in_progress":
        active_path = current_log_path
        has_active_path = active_path and any(str(item.get("_raw_path") or "") == active_path for item in phases)
        if active_path and not has_active_path:
            phase = str(task.get("run_phase") or "running")
            kind = _phase_kind(phase, str(task.get("agent") or ""))
            round_num = _phase_round(phase)
            if kind in {"builder", "reviewer"} and round_num is None:
                round_num = counters.get(kind, 0) + 1
            phases.append(
                _phase_entry_payload(
                    task=task,
                    entry={
                        "phase": phase,
                        "agent": task.get("agent") or "",
                        "started_at": task.get("heartbeat_at") or task.get("started_at") or "",
                        "finished_at": "",
                        "duration": None,
                        "exit_code": None,
                        "output": "",
                    },
                    index=len(phases),
                    kind=kind,
                    round_num=round_num,
                    raw_path=active_path,
                    active=True,
                    include_private=include_private,
                )
            )

    if include_private:
        return phases
    return [{key: value for key, value in item.items() if not key.startswith("_")} for item in phases]


def _bytes_delta(raw: bytes, *, offset: int = 0) -> dict:
    start = max(0, int(offset or 0))
    size = len(raw)
    if start >= size:
        return {"offset": start, "next_offset": size, "size": size, "text": "", "done": True}
    end = min(size, start + _LOG_CHUNK_MAX_BYTES)
    return {
        "offset": start,
        "next_offset": end,
        "size": size,
        "text": raw[start:end].decode("utf-8", errors="replace"),
        "done": end >= size,
    }


def task_phase_log_delta(task_id: int, phase_key: str, *, offset: int = 0) -> dict:
    """Return a lazy raw-log chunk for one task-detail phase key.

    The caller supplies only a stable phase key from ``task_detail_payload``.
    Paths are recomputed server-side from task logs/timeline artifacts and
    checked against CodePilot-owned roots before any file is read.
    """
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    project_path = task.get("project_path") or ""
    timeline = read_task_timeline_events(project_path, task_id) if project_path else []
    phases = _task_phase_logs(task, list(db.list_task_logs(task_id)), timeline, include_private=True)
    phase = next((item for item in phases if item.get("key") == phase_key), None)
    if not phase:
        raise RuntimeError(f"阶段日志 '{phase_key}' 不存在。")

    source = str(phase.get("raw_source") or "none")
    base = {
        "task_id": task_id,
        "key": phase_key,
        "source": source,
        "filename": str(phase.get("raw_filename") or ""),
    }
    if source == "file":
        path = Path(str(phase.get("_raw_path") or ""))
        try:
            with path.open("rb") as fh:
                fh.seek(max(0, int(offset or 0)))
                start = fh.tell()
                raw = fh.read(_LOG_CHUNK_MAX_BYTES)
            try:
                size = path.stat().st_size
            except OSError:
                size = start + len(raw)
            return {
                **base,
                "offset": start,
                "next_offset": start + len(raw),
                "size": size,
                "text": raw.decode("utf-8", errors="replace"),
                "done": start + len(raw) >= size,
            }
        except OSError:
            source = "none"
            base["source"] = source
    if source == "db":
        raw = str(phase.get("_output") or "").encode("utf-8", errors="replace")
        return {**base, **_bytes_delta(raw, offset=offset)}
    return {**base, "offset": max(0, int(offset or 0)), "next_offset": 0, "size": 0, "text": "", "done": True}


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


def _derive_recovery_hints(task: dict) -> list[str]:
    """Return user-facing recovery suggestions based on task state and error."""
    status = task.get("status") or ""
    error = task.get("error_message") or ""
    task_id = int(task.get("id") or 0)
    hints: list[str] = []
    lower_error = error.lower()
    builder_done = "Builder 已完成" in error
    reviewer_tool_failure = "Reviewer 工具失败" in error
    reviewer_timeout = "Reviewer" in error and ("超时" in error or "timeout" in lower_error)
    if builder_done and (reviewer_tool_failure or reviewer_timeout):
        hints.append(f"重试 review: codepilot task retry {task_id}")
        hints.append(f"接受 builder 结果: codepilot task done {task_id} -m \"review accepted\"")
        hints.append(f"切换 reviewer: codepilot task edit {task_id} --agent claude")
        hints.append("等待人工处理: 保留当前状态，由人工介入判断")
    elif "超时" in error or "timeout" in lower_error:
        hints.append(f"重试 review: codepilot task retry {task_id}")
        hints.append(f"接受 builder 结果: codepilot task done {task_id} -m \"review accepted\"")
        hints.append(f"切换 reviewer: codepilot task edit {task_id} --agent claude")
    if status == "backlog" and "review 命令执行失败" in error:
        hints.append(f"重试: codepilot task retry {task_id}")
    return hints


def _recent_task_update(task: dict) -> str:
    for key in ("completed_at", "heartbeat_at", "started_at", "created_at"):
        value = str(task.get(key) or "").strip()
        if value:
            return value
    return ""


def _execution_status(task: dict) -> str:
    status = str(task.get("status") or "").strip()
    phase = str(task.get("run_phase") or "").strip()
    if status and phase:
        return f"{status}:{phase}"
    return status or phase


def _task_ready_for_execution(task: dict) -> bool:
    content = str(task.get("content") or "")
    if not content.strip():
        return False
    return not missing_task_template_sections(content)


def _workflow_blocked_reason(task: dict, payload: dict) -> str:
    status = str(task.get("status") or "").strip()
    if payload.get("blocked_reason"):
        return str(payload["blocked_reason"])
    if payload.get("skip_reason"):
        return str(payload["skip_reason"])
    if payload.get("error_message"):
        return str(payload["error_message"])
    if status == "cancelled":
        return str(task.get("error_message") or task.get("stop_reason") or "任务已取消。")
    if status == "failed":
        return str(task.get("last_output") or "任务执行失败，请查看日志。")
    return ""


def _workflow_column_id(task: dict, payload: dict) -> str:
    status = str(task.get("status") or "").strip()
    phase = str(task.get("run_phase") or "").strip().lower()
    if status in {"done", "archived"}:
        return "done"
    if status in {"failed", "cancelled"}:
        return "blocked"
    if status in {"in_progress", "running"}:
        return "review" if "review" in phase else "running"
    if status == "backlog":
        if _workflow_blocked_reason(task, payload):
            return "blocked"
        return "ready" if _task_ready_for_execution(task) else "backlog"
    return "backlog"


def _task_payload(task: dict) -> dict:
    status = task["status"]

    # Best-effort ETA for this task based on historical median duration of
    # tasks run by the same agent in the same project. Only surfaces for
    # pending / in-progress tasks - done tasks show actual runtime instead.
    eta_seconds: int | None = None
    if status in {"backlog", "in_progress"}:
        try:
            eta_seconds = db.compute_agent_eta_seconds(task["project"], task.get("agent") or None)
        except Exception:
            eta_seconds = None

    # Preflight skip re-queues the task to backlog and stores the reason in
    # ``error_message``. That isn't a real failure - it's a "postponed, fix
    # this thing and I'll retry" warning. Surface it as ``skip_reason`` so
    # the UI can render it as a neutral / warning block instead of red.
    raw_error = task.get("error_message") or ""
    skip_reason = ""
    error_message = ""
    if status == "backlog" and raw_error:
        skip_reason = raw_error
    else:
        error_message = raw_error

    blocked_reason: str | None = None
    suggested_actions: list[str] | None = None
    if status == "backlog" and raw_error:
        from codepilot.commands.run_builtin_core import _preflight_blocked_detail
        detail = _preflight_blocked_detail(raw_error)
        if detail:
            blocked_reason = detail["reason"]
            suggested_actions = detail["suggested_actions"]

    payload = {
        "id": task["id"],
        "project": task["project"],
        "title": task["title"],
        "status": status,
        "priority": task["priority"],
        "agent": task["agent"],
        "source": task.get("source") or "user",
        "work_item": task_work_item_payload(task),
        "phase": task.get("run_phase") or "",
        "runtime": runtime_summary(task) if status == "in_progress" else "",
        "eta_seconds": eta_seconds,
        "latest": task.get("last_output") or skip_reason or error_message or task.get("delivery_record") or "",
        "error_message": error_message,
        "skip_reason": skip_reason,
        "blocked_reason": blocked_reason,
        "suggested_actions": suggested_actions,
        "delivery_record": task.get("delivery_record") or "",
        "created_at": task.get("created_at") or "",
        "started_at": task.get("started_at") or "",
        "completed_at": task.get("completed_at") or "",
        "updated_at": _recent_task_update(task),
        "execution_status": _execution_status(task),
        "retry_count": int(task.get("retry_count") or 0),
        "max_retries": int(task.get("max_retries") or 0),
        "recovery_hints": _derive_recovery_hints(task),
        "actions": {
            "retry": status in {"failed", "cancelled", "backlog"},
            "stop": status == "in_progress",
            "promote": status in {"backlog", "failed", "cancelled"},
            "cancel": status in {"backlog", "failed"},
            "archive": status == "done",
            "delete": status in {"backlog", "cancelled", "done", "archived"},
        },
    }
    column_id = _workflow_column_id(task, payload)
    payload["workflow_column_id"] = column_id
    payload["workflow_column_title"] = next(
        (column["title"] for column in WORKFLOW_BOARD_COLUMNS if column["id"] == column_id),
        column_id,
    )
    payload["blocked_reason"] = _workflow_blocked_reason(task, payload)
    return payload


def workflow_board_payload(project: str, tasks: list[dict]) -> dict:
    columns = {
        column["id"]: {
            **column,
            "count": 0,
            "tasks": [],
        }
        for column in WORKFLOW_BOARD_COLUMNS
    }
    total = 0
    for sort_index, task in enumerate(tasks):
        card = _task_payload(task)
        column_id = str(card.get("workflow_column_id") or "backlog")
        if column_id not in columns:
            column_id = "backlog"
            card["workflow_column_id"] = column_id
            card["workflow_column_title"] = "Backlog"
        card["sort_index"] = sort_index
        columns[column_id]["tasks"].append(card)
        columns[column_id]["count"] += 1
        total += 1

    return {
        "project": project,
        "columns": [columns[column_id] for column_id in _WORKFLOW_COLUMN_IDS],
        "counts": {column_id: columns[column_id]["count"] for column_id in _WORKFLOW_COLUMN_IDS},
        "total": total,
        "sort": {
            "column_order": list(_WORKFLOW_COLUMN_IDS),
            "task_order": "display_sort:status_priority_created_at_done_completed_desc",
        },
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
    execution_artifact = None
    artifacts: dict = {}
    project_path = task.get("project_path") or ""
    if project_path:
        execution_artifact = read_task_execution_artifacts(project_path, task_id)
        if execution_artifact:
            artifacts = dict(execution_artifact.get("artifacts") or {})
    timeline = read_task_timeline_events(project_path, task_id) if project_path else []
    phase_logs = _task_phase_logs(task, log_entries, timeline)
    if latest_review is not None and "review" not in artifacts:
        artifacts["review"] = {
            "kind": "review",
            "status": latest_review.get("verdict") or "unknown",
            "verdict": latest_review.get("verdict") or "unknown",
            "source": latest_review.get("source") or "",
            "summary": f"VERDICT: {str(latest_review.get('verdict') or 'unknown').upper()}",
            "blockers": list(latest_review.get("blockers") or []),
            "advisory": list(latest_review.get("advisory") or []),
            "ac_checks": list(latest_review.get("ac_checks") or []),
        }
    payload.update(
        {
            "content": task.get("content") or "",
            "depends_on": _parse_depends(task.get("depends_on")),
            "project_path": project_path,
            "current_log_path": task.get("current_log_path") or "",
            "log_text": _compose_log_text(task),
            "logs": logs,
            "phase_logs": phase_logs,
            "latest_review": latest_review,
            "artifacts": artifacts,
            "execution_artifact": execution_artifact,
            "timeline": timeline,
        }
    )
    return payload
