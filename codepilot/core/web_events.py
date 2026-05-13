"""Cross-process event delivery into the local Web UI process.

The in-process progress bus is enough for work started by the Web UI itself,
but project daemons and task runners live in independent processes. This
module gives those processes a tiny local transport: publish one JSON event to
the Web UI's internal HTTP endpoint when it is available.
"""

from __future__ import annotations

import http.client
import json
import os
from typing import Any

from codepilot.storage import database as db


DEFAULT_WEB_UI_HOST = "127.0.0.1"
DEFAULT_WEB_UI_PORT = 8766
WEBUI_SERVICE = "webui"
WEBUI_SCOPE = "_global"


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _web_event_delivery_allowed() -> bool:
    if _truthy_env("CODEPILOT_SUPPRESS_WEB_EVENTS"):
        return False
    if "PYTEST_CURRENT_TEST" in os.environ and not _truthy_env("CODEPILOT_ALLOW_TEST_WEB_EVENTS"):
        return False
    return True


def _webui_host_port() -> tuple[str, int]:
    state = db.get_service_state(WEBUI_SERVICE, WEBUI_SCOPE)
    meta = state.get("meta") if isinstance(state, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    host = str(meta.get("host") or DEFAULT_WEB_UI_HOST).strip() or DEFAULT_WEB_UI_HOST
    try:
        port = int(meta.get("port") or DEFAULT_WEB_UI_PORT)
    except Exception:
        port = DEFAULT_WEB_UI_PORT
    return host, port


def publish_web_event(payload: dict[str, Any], *, timeout: float = 0.25) -> bool:
    """Publish *payload* to the running Web UI event hub.

    Delivery is best-effort by design. If the Web UI is not running, task state
    still persists in SQLite and the dashboard can read a fresh snapshot on the
    next page load.
    """
    if not _web_event_delivery_allowed():
        return False
    host, port = _webui_host_port()
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection(host, int(port), timeout=max(float(timeout), 0.05))
    try:
        conn.request(
            "POST",
            "/internal/events",
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
            },
        )
        response = conn.getresponse()
        response.read()
        return 200 <= int(response.status) < 300
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _task_event_payload(
    *,
    project: str,
    task: dict,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    status: str = "",
    summary: str = "",
) -> dict[str, Any]:
    task_id = int(task.get("id") or 0)
    current = db.get_task(task_id) or task
    resolved_project = str(project or current.get("project") or task.get("project") or "")
    item = {
        "id": task_id,
        "title": str(current.get("title") or task.get("title") or ""),
        "project": resolved_project,
        "status": str(status or current.get("status") or ""),
        "run_phase": str(phase or current.get("run_phase") or ""),
        "completed_at": str(current.get("completed_at") or ""),
        "error_message": str(current.get("error_message") or ""),
    }
    changed_ids = [task_id] if task_id > 0 else []
    return {
        "task_id": task_id or None,
        "stage": "task-state",
        "type": str(event or "updated"),
        "level": str(level or "info"),
        "message": str(message or "任务状态已更新"),
        "extra": {
            "project": resolved_project,
            "event": str(event or "updated"),
            "phase": str(phase or ""),
            "status": item["status"],
            "summary": str(summary or ""),
            "changes": [{"type": str(event or "updated"), "task": item}],
            "changed_task_ids": changed_ids,
        },
    }


def publish_task_state_event(
    *,
    project: str,
    task: dict,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    status: str = "",
    summary: str = "",
) -> bool:
    """Publish a task state event to the Web UI event hub."""
    return publish_web_event(
        _task_event_payload(
            project=project,
            task=task,
            event=event,
            phase=phase,
            level=level,
            message=message,
            status=status,
            summary=summary,
        )
    )
