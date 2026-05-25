"""Read OpenCode's own project-scoped model selection state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from codepilot.storage import database as db


OPENCODE_MODEL_SERVICE = "opencode_model"
OPENCODE_BUILTIN_DEFAULT_PROVIDER = "opencode"
OPENCODE_BUILTIN_DEFAULT_MODEL = "minimax-m2.5-free"


def resolve_project_model_selection(
    project_scope: str,
    project_path: str | Path,
    *,
    db_path: str | Path | None = None,
) -> dict[str, str]:
    """Return CodePilot's saved project model, falling back to OpenCode session state."""
    saved = load_saved_project_model(project_scope)
    if saved and not _is_builtin_default_model(saved):
        return saved
    latest = load_latest_project_model(project_path, db_path=db_path)
    if _is_builtin_default_model(latest):
        return {}
    return latest


def load_saved_project_model(project_scope: str) -> dict[str, str]:
    """Return CodePilot's project-level OpenCode model selection."""
    scope = str(project_scope or "").strip()
    if not scope:
        return {}
    state = db.get_service_state(OPENCODE_MODEL_SERVICE, scope) or {}
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    if not isinstance(meta, dict):
        return {}
    return _normalize_model_payload(meta.get("model") or meta)


def save_project_model_selection(project_scope: str, model: Any) -> dict[str, str]:
    """Persist one project-level OpenCode model selection in CodePilot state."""
    scope = str(project_scope or "").strip()
    normalized = _normalize_model_payload(model)
    if not scope or not normalized:
        return {}
    db.init_db()
    db.upsert_service_state(
        OPENCODE_MODEL_SERVICE,
        scope,
        pid=0,
        status="active",
        log_path="",
        meta={
            "model": normalized,
            "provider_id": normalized["provider_id"],
            "model_id": normalized["model_id"],
            "variant": normalized.get("variant", ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return normalized


def sync_latest_project_model_selection(
    project_scope: str,
    project_path: str | Path,
    *,
    db_path: str | Path | None = None,
) -> dict[str, str]:
    """Persist the latest model OpenCode recorded for this project."""
    latest = load_latest_project_model(project_path, db_path=db_path)
    if not latest:
        return {}
    return save_project_model_selection(project_scope, latest)


def load_latest_project_model(
    project_path: str | Path,
    *,
    db_path: str | Path | None = None,
) -> dict[str, str]:
    """Return the latest model CodePilot-scoped OpenCode recorded for this project."""
    if db_path is None:
        return {}
    path = Path(db_path)
    if not path.is_file():
        return {}
    directory = str(Path(project_path).resolve())
    try:
        con = sqlite3.connect(path)
        row = con.execute(
            """
            select model
            from session
            where directory = ? and model is not null and trim(model) != ''
            order by time_updated desc
            limit 1
            """,
            (directory,),
        ).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass
    if not row:
        return {}
    return _normalize_model_payload(row[0])


def load_latest_project_session_id(
    project_path: str | Path,
    *,
    db_path: str | Path | None = None,
) -> str:
    """Return the latest CodePilot-scoped OpenCode session id for this project."""
    if db_path is None:
        return ""
    path = Path(db_path)
    if not path.is_file():
        return ""
    directory = str(Path(project_path).resolve())
    try:
        con = sqlite3.connect(path)
        row = con.execute(
            """
            select id
            from session
            where directory = ? and id is not null and trim(id) != ''
            order by time_updated desc
            limit 1
            """,
            (directory,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        try:
            con.close()
        except Exception:
            pass
    if not row:
        return ""
    return str(row[0] or "").strip()


def _normalize_model_payload(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(str(raw or ""))
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}
    provider_id = str(payload.get("providerID") or payload.get("provider_id") or "").strip()
    model_id = str(payload.get("id") or payload.get("modelID") or payload.get("model_id") or "").strip()
    variant = str(payload.get("variant") or "").strip()
    if not provider_id or not model_id:
        return {}
    result = {"provider_id": provider_id, "model_id": model_id}
    if variant:
        result["variant"] = variant
    return result


def _is_builtin_default_model(model: dict[str, str]) -> bool:
    return (
        str(model.get("provider_id") or "").strip().lower() == OPENCODE_BUILTIN_DEFAULT_PROVIDER
        and str(model.get("model_id") or "").strip().lower() == OPENCODE_BUILTIN_DEFAULT_MODEL
    )
