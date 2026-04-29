"""Project-local workflow mode state storage."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


_ACTIVE_STATE_FILE = "active-workflow.json"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def workflow_dirs(project_path: str | Path) -> dict[str, Path]:
    """Return the project-local workflow directory convention."""
    root = Path(project_path).expanduser().resolve() / ".codepilot"
    return {
        "root": root,
        "state": root / "state",
        "context": root / "context",
        "specs": root / "specs",
        "plans": root / "plans",
    }


def ensure_workflow_dirs(project_path: str | Path) -> dict[str, Path]:
    """Create workflow directories and return their paths."""
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
    return state


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
