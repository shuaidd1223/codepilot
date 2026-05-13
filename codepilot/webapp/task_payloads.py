"""Task-oriented payload builders for the Web UI."""

from __future__ import annotations

import json
from pathlib import Path

from codepilot.commands.reviewer_output import parse_reviewer_output
from codepilot.core.runtime import runtime_summary
from codepilot.storage import database as db
from codepilot.webapp.live_output_payloads import normalize_legacy_live_output_markdown


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

    # Legacy runs may still contain raw protocol under one ``~~~text`` fence
    # (no markdown sections inside Live Output). For completed tasks we can
    # safely normalize in-place so both API polling and direct file viewing
    # become markdown-native.
    try:
        if str(task.get("status") or "") != "in_progress":
            raw_full = target.read_text(encoding="utf-8", errors="replace")
            normalized = normalize_legacy_live_output_markdown(raw_full)
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

