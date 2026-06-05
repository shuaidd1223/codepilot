"""Project-local workflow mode state storage."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_ACTIVE_STATE_FILE = "active-workflow.json"
_AGENT_SESSION_FILE = "agent-session.json"
TASK_TIMELINE_EVENTS = frozenset(
    {
        "created",
        "planned",
        "claimed",
        "agent_started",
        "diff_detected",
        "validated",
        "reviewed",
        "supervised",
        "blocked",
        "done",
        "failed",
    }
)
_TIMELINE_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|APP[_-]?SECRET|PRIVATE[_-]?KEY)[A-Z0-9_.-]*)"
    r"\s*([:=])\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_TIMELINE_BEARER_RE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/\-]+=*", re.IGNORECASE)
_TIMELINE_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")

# Tracks project paths where .git/info/exclude has already been patched
# to reduce redundant I/O on hot paths (append_task_timeline_event, etc.).
_git_exclude_ensured: set[Path] = set()

# Public aliases for sharing across modules (supervisor, etc.)
SECRET_ASSIGNMENT_RE = _TIMELINE_SECRET_ASSIGNMENT_RE
BEARER_RE = _TIMELINE_BEARER_RE
SK_RE = _TIMELINE_SK_RE


@contextmanager
def _file_lock(lock_path: Path, timeout: float = 10.0):
    """Cross-platform exclusive file lock using a lockfile."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Could not acquire lock: {lock_path}")
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            os.unlink(str(lock_path))
        except FileNotFoundError:
            pass


@contextmanager
def _locked_file(path, mode="r+"):
    """Open path, acquire a file lock via lockfile, yield the file object, then unlock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}\n", encoding="utf-8")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _file_lock(lock_path):
        with open(path, mode, encoding="utf-8") as fp:
            yield fp


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workflow_dirs(project_path: str | Path) -> dict[str, Path]:
    """Return the project-local workflow directory convention."""
    root = Path(project_path).expanduser().resolve() / ".codepilot"
    return {
        "root": root,
        "state": root / "state",
        "context": root / "context",
        "specs": root / "specs",
        "plans": root / "plans",
        "artifacts": root / "artifacts",
    }


def _git_info_dir(project_root: Path) -> Path | None:
    dotgit = project_root / ".git"
    if dotgit.is_dir():
        return dotgit / "info"
    if not dotgit.is_file():
        return None
    try:
        text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.lower().startswith(prefix):
        return None
    raw_git_dir = text[len(prefix):].strip()
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        git_dir = (project_root / git_dir).resolve()
    return git_dir / "info"


def _ensure_codepilot_git_excluded(project_path: str | Path) -> None:
    """Keep project-local CodePilot artifacts out of user worktree status."""
    project_root = Path(project_path).expanduser().resolve()
    if project_root in _git_exclude_ensured:
        return
    info_dir = _git_info_dir(project_root)
    if info_dir is None:
        return
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_path = info_dir / "exclude"
        text = exclude_path.read_text(encoding="utf-8", errors="replace") if exclude_path.exists() else ""
        entries = {line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")}
        if ".codepilot/" in entries or ".codepilot" in entries or "/.codepilot/" in entries:
            _git_exclude_ensured.add(project_root)
            return
        separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
        exclude_path.write_text(f"{text}{separator}.codepilot/\n", encoding="utf-8")
        _git_exclude_ensured.add(project_root)
    except OSError:
        return


def ensure_workflow_dirs(project_path: str | Path) -> dict[str, Path]:
    """Create workflow directories and return their paths."""
    _ensure_codepilot_git_excluded(project_path)
    dirs = workflow_dirs(project_path)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _mode_state_path(project_path: str | Path, mode: str) -> Path:
    clean_mode = str(mode or "").strip().lower()
    if not clean_mode:
        raise ValueError("workflow mode is required")
    return workflow_dirs(project_path)["state"] / f"{clean_mode}-state.json"


def _active_state_path(project_path: str | Path) -> Path:
    return workflow_dirs(project_path)["state"] / _ACTIVE_STATE_FILE


def task_execution_artifact_path(project_path: str | Path, task_id: int | str) -> Path:
    """Return the project-local execution artifact summary path for one task."""
    try:
        clean_task_id = int(task_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("task_id must be an integer") from exc
    if clean_task_id <= 0:
        raise ValueError("task_id must be positive")
    return workflow_dirs(project_path)["artifacts"] / "tasks" / f"task-{clean_task_id}-execution.json"


def _default_artifact_paths(dirs: dict[str, Path], session_id: str) -> dict[str, str]:
    return {
        "context": str(dirs["context"] / f"{session_id}.json"),
        "spec": str(dirs["specs"] / f"{session_id}.md"),
        "plan": str(dirs["plans"] / f"{session_id}.md"),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _coerce_execution_artifacts(raw_artifacts: Any) -> dict[str, Any]:
    if not isinstance(raw_artifacts, dict):
        return {}
    artifacts: dict[str, Any] = {}
    for key in ("patch", "validation", "review"):
        value = raw_artifacts.get(key)
        if isinstance(value, dict):
            item = dict(value)
            item["kind"] = str(item.get("kind") or key)
            artifacts[key] = item
    return artifacts


def _compact_timeline_text(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _redact_timeline_secrets(text: str) -> str:
    text = _TIMELINE_SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    text = _TIMELINE_BEARER_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    return _TIMELINE_SK_RE.sub("[redacted]", text)


def _safe_timeline_text(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return _redact_timeline_secrets(text)[:limit]


def _coerce_task_timeline(raw_timeline: Any) -> list[dict[str, str]]:
    if not isinstance(raw_timeline, list):
        return []
    events: list[dict[str, str]] = []
    for item in raw_timeline:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or item.get("type") or "").strip().lower()
        if event not in TASK_TIMELINE_EVENTS:
            continue
        timestamp = _compact_timeline_text(item.get("time") or item.get("timestamp"), limit=80)
        if not timestamp:
            continue
        events.append(
            {
                "time": timestamp,
                "event": event,
                "actor": _safe_timeline_text(item.get("actor"), limit=80),
                "source": _safe_timeline_text(item.get("source"), limit=120),
                "message": _safe_timeline_text(item.get("message"), limit=500),
                "artifact_path": _safe_timeline_text(item.get("artifact_path"), limit=500),
            }
        )
    return events


def read_task_execution_artifacts(project_path: str | Path, task_id: int | str) -> dict[str, Any] | None:
    """Read a task execution artifact summary; corrupt or missing returns None."""
    path = task_execution_artifact_path(project_path, task_id)
    payload = _read_json(path)
    if payload is None:
        return None
    try:
        payload["task_id"] = int(payload.get("task_id") or task_id)
    except (TypeError, ValueError):
        payload["task_id"] = int(task_id)
    payload["artifact_path"] = str(path)
    payload["artifacts"] = _coerce_execution_artifacts(payload.get("artifacts"))
    payload["timeline"] = _coerce_task_timeline(payload.get("timeline"))
    return payload


def read_task_timeline_events(project_path: str | Path, task_id: int | str) -> list[dict[str, str]]:
    """Read the compact per-task audit timeline; missing legacy data returns an empty list."""
    payload = _read_json(task_execution_artifact_path(project_path, task_id))
    if payload is None:
        return []
    return _coerce_task_timeline(payload.get("timeline"))


def append_task_timeline_event(
    project_path: str | Path,
    task_id: int | str,
    *,
    event: str,
    actor: str = "",
    source: str = "",
    message: str = "",
    artifact_path: str | Path | None = None,
    time: str | None = None,
) -> dict[str, str]:
    """Append one compact task timeline event to the project-local artifact.

    Timeline is an audit trail only. It stores short facts and file pointers,
    never large command output or model transcripts.
    """
    clean_event = str(event or "").strip().lower()
    if clean_event not in TASK_TIMELINE_EVENTS:
        raise ValueError(f"unsupported task timeline event: {event!r}")

    _ensure_codepilot_git_excluded(project_path)
    path = task_execution_artifact_path(project_path, task_id)
    now = _now_iso()
    record = {
        "time": _compact_timeline_text(time or now, limit=80),
        "event": clean_event,
        "actor": _safe_timeline_text(actor, limit=80),
        "source": _safe_timeline_text(source, limit=120),
        "message": _safe_timeline_text(message, limit=500),
        "artifact_path": _safe_timeline_text(artifact_path, limit=500),
    }
    with _locked_file(path) as fp:
        try:
            current = json.load(fp)
        except (json.JSONDecodeError, UnicodeDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        timeline = _coerce_task_timeline(current.get("timeline"))
        timeline.append(record)
        payload: dict[str, Any] = {
            "task_id": int(task_id),
            "status": str(current.get("status") or ""),
            "source": str(current.get("source") or ""),
            "executor": str(current.get("executor") or ""),
            "created_at": str(current.get("created_at") or now),
            "updated_at": now,
            "artifact_path": str(path),
            "artifacts": _coerce_execution_artifacts(current.get("artifacts")),
            "metadata": dict(current.get("metadata") or {}) if isinstance(current.get("metadata"), dict) else {},
            "timeline": timeline,
        }
        fp.seek(0)
        fp.truncate()
        json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
        fp.flush()
    return record


def _artifact_summary(artifact: dict[str, Any], *, fallback: str) -> str:
    summary = _compact_timeline_text(artifact.get("summary"), limit=220)
    if summary:
        return summary
    status = _compact_timeline_text(artifact.get("status") or artifact.get("verdict"), limit=80)
    return f"{fallback}: {status}" if status else fallback


def _append_artifact_timeline_events(
    project_path: str | Path,
    task_id: int,
    artifacts: dict[str, Any],
    artifact_path: Path,
) -> None:
    patch = artifacts.get("patch")
    if isinstance(patch, dict) and not patch.get("empty"):
        append_task_timeline_event(
            project_path,
            task_id,
            event="diff_detected",
            actor="runner",
            source="codepilot.execution_artifact",
            message=_artifact_summary(patch, fallback="Diff detected"),
            artifact_path=str(artifact_path),
        )
    validation = artifacts.get("validation")
    if isinstance(validation, dict):
        append_task_timeline_event(
            project_path,
            task_id,
            event="validated",
            actor="runner",
            source="codepilot.execution_artifact",
            message=_artifact_summary(validation, fallback="Validation recorded"),
            artifact_path=str(artifact_path),
        )
    review = artifacts.get("review")
    if isinstance(review, dict):
        append_task_timeline_event(
            project_path,
            task_id,
            event="reviewed",
            actor="reviewer",
            source="codepilot.execution_artifact",
            message=_artifact_summary(review, fallback="Review recorded"),
            artifact_path=str(artifact_path),
        )


def write_task_execution_artifacts(
    project_path: str | Path,
    task_id: int | str,
    *,
    status: str = "",
    source: str = "",
    executor: str = "",
    artifacts: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge and persist patch/validation/review summaries for a task.

    The payload intentionally stores compact metadata and pointers only. Full
    diffs and large logs stay in git/log files, which keeps this safe for the
    direct-workspace execution mode and avoids a database migration.
    """
    _ensure_codepilot_git_excluded(project_path)
    path = task_execution_artifact_path(project_path, task_id)
    now = _now_iso()
    with _locked_file(path) as fp:
        try:
            current = json.load(fp)
        except (json.JSONDecodeError, UnicodeDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        merged_artifacts = _coerce_execution_artifacts(current.get("artifacts"))
        merged_artifacts.update(_coerce_execution_artifacts(artifacts or {}))
        merged_metadata = dict(current.get("metadata") or {}) if isinstance(current.get("metadata"), dict) else {}
        if metadata:
            merged_metadata.update(dict(metadata))
        timeline = _coerce_task_timeline(current.get("timeline"))

        clean_task_id = int(task_id)
        payload: dict[str, Any] = {
            "task_id": clean_task_id,
            "status": str(status or current.get("status") or ""),
            "source": str(source or current.get("source") or ""),
            "executor": str(executor or current.get("executor") or ""),
            "created_at": str(current.get("created_at") or now),
            "updated_at": now,
            "artifact_path": str(path),
            "artifacts": merged_artifacts,
            "metadata": merged_metadata,
            "timeline": timeline,
        }
        fp.seek(0)
        fp.truncate()
        json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
        fp.flush()
    try:
        _append_artifact_timeline_events(project_path, clean_task_id, _coerce_execution_artifacts(artifacts or {}), path)
    except Exception:
        pass
    return payload


def _write_state_files(project_path: str | Path, state: dict[str, Any]) -> dict[str, Any]:
    mode = str(state.get("mode") or "").strip().lower()
    if not mode:
        raise ValueError("workflow state requires mode")
    dirs = ensure_workflow_dirs(project_path)
    state = dict(state)
    state["mode"] = mode
    _atomic_write_json(dirs["state"] / f"{mode}-state.json", state)

    active_path = dirs["state"] / _ACTIVE_STATE_FILE
    if state.get("active") is True:
        _atomic_write_json(active_path, state)
    else:
        active = _read_json(active_path)
        if active and str(active.get("mode") or "").strip().lower() == mode:
            active_path.unlink(missing_ok=True)
    _publish_workflow_changed_event(project_path, state)
    return state


def _publish_workflow_changed_event(project_path: str | Path, state: dict[str, Any]) -> None:
    try:
        from codepilot.core.event_plugins import build_event, dispatch_event_to_sinks
        from codepilot.storage import database as db

        root = Path(project_path).expanduser().resolve()
        project = db.find_project_by_path(root)
        if not project:
            return
        mode = str(state.get("mode") or "").strip().lower()
        if not mode:
            return
        state_path = (Path(".codepilot") / "state" / f"{mode}-state.json").as_posix()
        event = build_event(
            str(project.get("name") or ""),
            "workflow.changed",
            source="codepilot.workflow",
            payload={
                "mode": mode,
                "phase": str(state.get("current_phase") or ""),
                "active": bool(state.get("active")),
                "session_id": str(state.get("session_id") or ""),
                "state_path": state_path,
                "context_path": str(state.get("context_path") or ""),
                "artifact_paths": dict(state.get("artifact_paths") or {}),
            },
            event_id_prefix=f"workflow-{mode}",
        )
        dispatch_event_to_sinks(root, event)
    except Exception:
        return


def _agent_session_path(project_path: str | Path) -> Path:
    return workflow_dirs(project_path)["state"] / _AGENT_SESSION_FILE


def create_agent_session(
    project_path: str | Path,
    *,
    goal: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Create a new agent workflow session starting at the intake phase."""
    dirs = ensure_workflow_dirs(project_path)
    now = _now_iso()
    sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
    artifacts = _default_artifact_paths(dirs, sid)
    session: dict[str, Any] = {
        "session_id": sid,
        "goal": goal,
        "current_phase": "intake",
        "phase_history": [{"phase": "intake", "entered_at": now, "exited_at": None}],
        "blocked_reason": None,
        "next_actions": [],
        "next_action_details": [],
        "linked_task_ids": [],
        "artifact_paths": artifacts,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    _atomic_write_json(_agent_session_path(project_path), session)
    return session


def _next_action_ids(raw_actions: Any) -> list[str]:
    if not isinstance(raw_actions, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for item in raw_actions:
        if isinstance(item, dict):
            action_id = str(item.get("id") or "").strip()
        else:
            action_id = str(item or "").strip()
        if not action_id or action_id in seen:
            continue
        seen.add(action_id)
        ids.append(action_id)
    return ids


def _next_action_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id") or item.get("action_id") or "").strip()
    return str(item or "").strip() if isinstance(item, str) else ""


def normalize_workflow_next_actions(raw_actions: Any) -> list[dict[str, Any]]:
    """Normalize legacy string actions and structured actions into dicts."""
    actions: list[dict[str, Any]] = []
    if not isinstance(raw_actions, list):
        return actions
    for item in raw_actions:
        if isinstance(item, dict):
            action_id = _next_action_id(item)
            if not action_id:
                continue
            action = dict(item)
            action["id"] = action_id
            action["label"] = str(action.get("label") or action_id)
            action["risk"] = str(action.get("risk") or "unknown").lower()
            actions.append(action)
        elif isinstance(item, str) and item.strip():
            text = item.strip()
            actions.append({"id": text, "label": text, "risk": "unknown", "suggested_command": ""})
    return actions


def _iter_action_history_payloads(payload: Any):
    if not isinstance(payload, dict):
        return
    yield payload
    nested_state = payload.get("state")
    if isinstance(nested_state, dict):
        yield nested_state


def _payload_context_paths(payload: Any) -> set[str]:
    paths: set[str] = set()
    if not isinstance(payload, dict):
        return paths
    raw_context = str(payload.get("context_path") or "").strip()
    if raw_context:
        paths.add(raw_context)
    artifacts = payload.get("artifact_paths")
    if isinstance(artifacts, dict):
        artifact_context = str(artifacts.get("context") or "").strip()
        if artifact_context:
            paths.add(artifact_context)
    nested_state = payload.get("state")
    if isinstance(nested_state, dict):
        paths.update(_payload_context_paths(nested_state))
    return paths


def _consumed_record_context_path(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    source = item.get("source")
    if isinstance(source, dict):
        return str(source.get("context_path") or "").strip()
    return ""


def consumed_workflow_action_ids(*payloads: Any) -> set[str]:
    """Return action ids persisted as consumed or expired in compatible payloads."""
    ids: set[str] = set()
    target_context_paths: set[str] = set()
    for payload in payloads:
        target_context_paths.update(_payload_context_paths(payload))
    for payload in payloads:
        for source in _iter_action_history_payloads(payload):
            for key in ("consumed_actions", "action_history"):
                raw_items = source.get(key)
                if not isinstance(raw_items, list):
                    continue
                for item in raw_items:
                    action_id = _next_action_id(item)
                    if not action_id:
                        continue
                    if key == "action_history" and isinstance(item, dict):
                        status = str(item.get("status") or "").strip().lower()
                        if status and status not in {"consumed", "expired"}:
                            continue
                    record_context = _consumed_record_context_path(item)
                    if target_context_paths and record_context and record_context not in target_context_paths:
                        continue
                    ids.add(action_id)
    return ids


def filter_consumed_next_action_items(raw_actions: Any, *payloads: Any) -> list[Any]:
    """Filter next action items while preserving their original item shape."""
    if not isinstance(raw_actions, list):
        return []
    consumed = consumed_workflow_action_ids(*payloads)
    if not consumed:
        return [dict(item) if isinstance(item, dict) else item for item in raw_actions]
    filtered: list[Any] = []
    for item in raw_actions:
        action_id = _next_action_id(item)
        if not action_id or action_id in consumed:
            continue
        filtered.append(dict(item) if isinstance(item, dict) else item)
    return filtered


def filter_consumed_workflow_next_actions(raw_actions: Any, *payloads: Any) -> list[dict[str, Any]]:
    """Normalize next actions after applying persisted consumed/expired records."""
    return normalize_workflow_next_actions(filter_consumed_next_action_items(raw_actions, *payloads))


def workflow_action_ids_consumed_by(action_id: str, extra_action_ids: list[str] | None = None) -> list[str]:
    """Return the action ids that should disappear after a successful action."""
    primary = str(action_id or "").strip()
    ids: list[str] = []
    seen: set[str] = set()
    for candidate in [primary, *(extra_action_ids or [])]:
        text = str(candidate or "").strip()
        if text and text not in seen:
            seen.add(text)
            ids.append(text)
    if primary == "import_tasks" and "execute_directly" not in seen:
        ids.append("execute_directly")
    return ids


def _normalize_consumed_records(raw_records: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_records, list):
        return []
    records: list[dict[str, Any]] = []
    for item in raw_records:
        action_id = _next_action_id(item)
        if not action_id:
            continue
        if isinstance(item, dict):
            record = dict(item)
            record["id"] = action_id
        else:
            record = {"id": action_id}
        records.append(record)
    return records


def _merge_consumed_records(payload: dict[str, Any], new_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    replace_ids = {str(item.get("id") or "") for item in new_records}
    merged = [
        item
        for item in _normalize_consumed_records(payload.get("consumed_actions"))
        if str(item.get("id") or "") not in replace_ids
    ]
    merged.extend(new_records)
    return merged


def _consumed_records(
    action_id: str,
    *,
    source: dict[str, Any] | None,
    extra_action_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    now = _now_iso()
    primary = str(action_id or "").strip()
    records: list[dict[str, Any]] = []
    for item_id in workflow_action_ids_consumed_by(primary, extra_action_ids):
        record: dict[str, Any] = {
            "id": item_id,
            "consumed_at": now,
            "source": dict(source or {}),
            "status": "consumed" if item_id == primary else "expired",
        }
        if item_id != primary:
            record["superseded_by"] = primary
        records.append(record)
    return records


def _resolve_context_path(project_path: Path, raw_path: str | Path | None) -> Path | None:
    if raw_path is None or str(raw_path).strip() == "":
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = project_path / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(project_path):
        return None
    return resolved


def mark_workflow_actions_consumed(
    project_path: str | Path,
    *,
    action_id: str,
    mode: str | None = None,
    context_path: str | Path | None = None,
    source: dict[str, Any] | None = None,
    extra_action_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Persist successful workflow action consumption in context/state files."""
    project_root = Path(project_path).expanduser().resolve()
    records = _consumed_records(action_id, source=source, extra_action_ids=extra_action_ids)
    if not records:
        return {"consumed_actions": []}

    resolved_context = _resolve_context_path(project_root, context_path)
    context_payload: dict[str, Any] | None = None
    if resolved_context and resolved_context.is_file():
        context_payload = _read_json(resolved_context) or {}
        context_payload["consumed_actions"] = _merge_consumed_records(context_payload, records)
        nested_state = context_payload.get("state")
        if isinstance(nested_state, dict):
            nested_state["consumed_actions"] = _merge_consumed_records(nested_state, records)
        _atomic_write_json(resolved_context, context_payload)

    clean_mode = str(mode or "").strip().lower()
    if not clean_mode and context_payload:
        nested_state = context_payload.get("state")
        if isinstance(nested_state, dict):
            clean_mode = str(nested_state.get("mode") or "").strip().lower()
        if not clean_mode:
            clean_mode = str(context_payload.get("artifact_type") or "").strip().lower()

    if clean_mode:
        state = read_workflow_state(project_root, mode=clean_mode)
        if state is not None:
            update_workflow_state(
                project_root,
                clean_mode,
                consumed_actions=_merge_consumed_records(state, records),
            )

    session = get_agent_session(project_root)
    if session is not None:
        update_agent_session(
            project_root,
            consumed_actions=_merge_consumed_records(session, records),
        )

    return {"consumed_actions": records}


def workflow_payload_with_consumable_actions(
    payload: dict[str, Any] | None,
    *extra_payloads: Any,
) -> dict[str, Any] | None:
    """Return a payload copy whose next actions exclude consumed/expired records."""
    if payload is None:
        return None
    view = dict(payload)
    filter_payloads = (view, *extra_payloads)
    if isinstance(view.get("next_actions"), list):
        view["next_actions"] = filter_consumed_next_action_items(view.get("next_actions"), *filter_payloads)
    if isinstance(view.get("next_action_details"), list):
        view["next_action_details"] = filter_consumed_workflow_next_actions(
            view.get("next_action_details"),
            *filter_payloads,
        )
    if isinstance(view.get("phase_history"), list):
        phase_history: list[Any] = []
        for entry in view.get("phase_history") or []:
            if not isinstance(entry, dict):
                phase_history.append(entry)
                continue
            entry_view = dict(entry)
            mode_state = entry_view.get("mode_state")
            if isinstance(mode_state, dict):
                mode_context = str(mode_state.get("context_path") or "").strip()
                inherited_consumption: list[dict[str, Any]] = []
                for payload in filter_payloads:
                    if not isinstance(payload, dict):
                        continue
                    inherited: dict[str, Any] = {"context_path": mode_context}
                    if isinstance(payload.get("consumed_actions"), list):
                        inherited["consumed_actions"] = payload.get("consumed_actions")
                    if isinstance(payload.get("action_history"), list):
                        inherited["action_history"] = payload.get("action_history")
                    if len(inherited) > 1:
                        inherited_consumption.append(inherited)
                entry_view["mode_state"] = workflow_payload_with_consumable_actions(
                    mode_state,
                    *inherited_consumption,
                )
            phase_history.append(entry_view)
        view["phase_history"] = phase_history
    return view


def _coerce_artifact_paths(raw_paths: dict[str, str | Path] | None) -> dict[str, str]:
    if not raw_paths:
        return {}
    paths: dict[str, str] = {}
    for key, value in raw_paths.items():
        text = str(value or "").strip()
        if text:
            paths[str(key)] = text
    return paths


def get_agent_session(project_path: str | Path) -> dict[str, Any] | None:
    """Read the current agent session; corrupt or missing returns None."""
    return _read_json(_agent_session_path(project_path))


def ensure_agent_session(
    project_path: str | Path,
    *,
    goal: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Return the active project-local agent session, creating one if needed."""
    session = get_agent_session(project_path)
    if session is None or session.get("completed_at"):
        return create_agent_session(project_path, goal=goal, session_id=session_id)
    if goal and not str(session.get("goal") or "").strip():
        updated = update_agent_session(project_path, goal=goal)
        return updated or session
    return session


def update_agent_session(project_path: str | Path, **changes: Any) -> dict[str, Any] | None:
    """Update agent session fields in-place; returns None if no session exists."""
    session = get_agent_session(project_path)
    if session is None:
        return None
    now = _now_iso()
    session.update(changes)
    session["updated_at"] = now
    if changes.get("completed_at"):
        session["phase_history"][-1]["exited_at"] = now
    _atomic_write_json(_agent_session_path(project_path), session)
    return session


def advance_or_update_agent_phase(
    project_path: str | Path,
    phase: str,
    *,
    goal: str,
    artifact_paths: dict[str, str | Path] | None = None,
    next_actions: list[Any] | None = None,
    next_action_details: list[dict[str, Any]] | None = None,
    mode_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create/reuse the agent session and sync a workflow phase into it."""
    session = ensure_agent_session(project_path, goal=goal)
    paths = dict(session.get("artifact_paths") or {})
    paths.update(_coerce_artifact_paths(artifact_paths))

    changes: dict[str, Any] = {"artifact_paths": paths}
    if next_actions is not None:
        changes["next_actions"] = _next_action_ids(next_actions)
    if next_action_details is not None:
        changes["next_action_details"] = list(next_action_details)
    elif next_actions is not None and any(isinstance(item, dict) for item in next_actions):
        changes["next_action_details"] = [dict(item) for item in next_actions if isinstance(item, dict)]

    if session.get("current_phase") == phase:
        return update_agent_session(project_path, **changes)
    if mode_state is not None:
        changes["mode_state"] = mode_state
    return advance_agent_phase(project_path, phase, **changes)


def advance_agent_phase(project_path: str | Path, phase: str, **extra_fields: Any) -> dict[str, Any] | None:
    """Advance to a new phase, closing the previous one in phase_history."""
    session = get_agent_session(project_path)
    if session is None:
        return None
    now = _now_iso()
    if session["phase_history"]:
        session["phase_history"][-1]["exited_at"] = now
    entry: dict[str, Any] = {"phase": phase, "entered_at": now, "exited_at": None}
    if "mode_state" in extra_fields:
        entry["mode_state"] = extra_fields.pop("mode_state")
    session["phase_history"].append(entry)
    session["current_phase"] = phase
    session["updated_at"] = now
    session.update(extra_fields)
    _atomic_write_json(_agent_session_path(project_path), session)
    return session


def fail_agent_session(
    project_path: str | Path,
    *,
    blocked_reason: str,
    next_actions: list[str] | None = None,
) -> dict[str, Any] | None:
    """Mark session as blocked with a reason and suggested next actions."""
    return update_agent_session(
        project_path,
        blocked_reason=blocked_reason,
        next_actions=next_actions or [],
    )


def complete_agent_session(project_path: str | Path) -> dict[str, Any] | None:
    """Mark session as completed and close the final phase."""
    session = get_agent_session(project_path)
    if session is None:
        return None
    now = _now_iso()
    session["completed_at"] = now
    session["current_phase"] = "completed"
    session["updated_at"] = now
    if session["phase_history"]:
        session["phase_history"][-1]["exited_at"] = now
    _atomic_write_json(_agent_session_path(project_path), session)
    return session


def cleanup_agent_session(project_path: str | Path) -> bool:
    """Remove the agent session state file. Returns True if removed."""
    path = _agent_session_path(project_path)
    if path.exists():
        path.unlink(missing_ok=True)
        return True
    return False


def start_workflow(
    project_path: str | Path,
    *,
    mode: str,
    session_id: str | None = None,
    current_phase: str = "started",
    context_path: str | Path | None = None,
    artifact_paths: dict[str, str | Path] | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Create an active workflow state for one mode."""
    dirs = ensure_workflow_dirs(project_path)
    now = started_at or _now_iso()
    sid = session_id or uuid.uuid4().hex[:12]
    artifacts = _default_artifact_paths(dirs, sid)
    if artifact_paths:
        artifacts.update({str(key): str(value) for key, value in artifact_paths.items()})
    resolved_context_path = str(context_path) if context_path is not None else artifacts["context"]
    artifacts["context"] = resolved_context_path
    state: dict[str, Any] = {
        "mode": str(mode).strip().lower(),
        "active": True,
        "current_phase": current_phase,
        "session_id": sid,
        "context_path": resolved_context_path,
        "artifact_paths": artifacts,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    return _write_state_files(project_path, state)


def read_workflow_state(project_path: str | Path, *, mode: str | None = None) -> dict[str, Any] | None:
    """Read active state or a mode-specific state; invalid JSON returns None."""
    path = _mode_state_path(project_path, mode) if mode else _active_state_path(project_path)
    return _read_json(path)


def update_workflow_state(project_path: str | Path, mode: str, **changes: Any) -> dict[str, Any]:
    """Update an existing mode state, creating an active state if missing."""
    current = read_workflow_state(project_path, mode=mode)
    if current is None:
        current = start_workflow(project_path, mode=mode)
    next_state = dict(current)
    next_state.update(changes)
    next_state["mode"] = str(mode).strip().lower()
    next_state["updated_at"] = changes.get("updated_at") or _now_iso()
    if next_state.get("active") is False and not next_state.get("completed_at"):
        next_state["completed_at"] = next_state["updated_at"]
    return _write_state_files(project_path, next_state)


def complete_workflow(
    project_path: str | Path,
    mode: str,
    *,
    completed_at: str | None = None,
) -> dict[str, Any]:
    """Mark one workflow mode complete and clear it from the active pointer."""
    when = completed_at or _now_iso()
    return update_workflow_state(
        project_path,
        mode,
        active=False,
        current_phase="completed",
        completed_at=when,
        updated_at=when,
    )


def cleanup_workflow_states(project_path: str | Path, *, completed: bool = True) -> list[Path]:
    """Remove inactive completed mode states and stale inactive active pointers."""
    state_dir = workflow_dirs(project_path)["state"]
    if not state_dir.exists():
        return []

    removed: list[Path] = []
    for path in sorted(state_dir.glob("*-state.json")):
        state = _read_json(path)
        if not state:
            continue
        if completed and state.get("active") is False and state.get("completed_at"):
            path.unlink(missing_ok=True)
            removed.append(path)

    active_path = state_dir / _ACTIVE_STATE_FILE
    active = _read_json(active_path)
    if active and active.get("active") is not True:
        active_path.unlink(missing_ok=True)
        removed.append(active_path)
    return removed
