"""Local Web UI for browsing and operating CodePilot projects and tasks.

The implementation is split across two companion modules:

- :mod:`codepilot.webui_payloads` -- pure payload-building helpers.
- :mod:`codepilot.webui_actions` -- state-mutating actions backing the HTTP API.

This module owns the HTTP handler, the ``ThreadingHTTPServer`` bootstrap, and
the shared in-memory UI state (``_UI_LOCK`` / ``_UI_JOB_SEQ`` / ``_UI_JOBS`` /
``_UI_EVENTS``). State lives here so that tests which reset it via
``webui_mod._UI_JOBS.clear()`` / ``webui_mod._UI_JOB_SEQ = 0`` keep working --
every action looks state up through this shell at call time.

Every public function from the companion modules is re-exported so existing
imports (``from codepilot.webui import submit_requirement_action``,
``from codepilot.webui import dashboard_payload``, etc.) remain valid.
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from codepilot import db
# Re-exported so tests that monkeypatch ``webui_mod.run_requirement_workflow``
# drive :func:`submit_requirement_action` end-to-end.
from codepilot.commands.auto import run_requirement_workflow  # noqa: F401 (re-export)
from codepilot.webui_actions import (  # noqa: F401 (re-export)
    _GOAL_MAX_BYTES,
    _MAX_EVENTS,
    _MAX_JOB_LOG_LINES,
    _append_event,
    _job_result_summary,
    _next_job_id,
    _update_job,
    create_session_action,
    create_task_action,
    delete_session_action,
    get_session_action,
    list_sessions_action,
    list_ui_events,
    list_ui_jobs,
    promote_task_action,
    retry_task_action,
    send_session_message_action,
    stop_task_action,
    submit_goal_action,
    submit_requirement_action,
)
from codepilot.webui_payloads import (  # noqa: F401 (re-export)
    STATUS_ORDER,
    _compose_log_text,
    _now_iso,
    _parse_depends,
    _read_text,
    _sorted_tasks,
    _tail_text,
    _task_payload,
    dashboard_payload,
    project_summary,
    task_detail_payload,
)


# ── Shared in-memory UI state (read/written via this shell by webui_actions) ──
_UI_LOCK = threading.Lock()
_UI_JOB_SEQ = 0
_UI_JOBS: dict[int, dict] = {}
_UI_EVENTS: list[dict] = []


# ── Static web assets ────────────────────────────────────────────────────────
_WEB_DIR = Path(__file__).parent / "web"


def _load_web_file(name: str) -> bytes:
    path = _WEB_DIR / name
    return path.read_bytes()


def _safe_web_path(rel: str) -> Path | None:
    """Resolve *rel* under _WEB_DIR, preventing path traversal."""
    try:
        candidate = (_WEB_DIR / rel).resolve()
        base = _WEB_DIR.resolve()
        candidate.relative_to(base)
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodePilotUI/0.2"

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_html_file(self, name: str = "index.html") -> None:
        try:
            body = _load_web_file(name)
        except FileNotFoundError:
            self._send_json({"error": f"找不到 {name}"}, status=500)
            return
        self._send_bytes(body, "text/html; charset=utf-8")

    def _serve_static(self, rel: str) -> bool:
        target = _safe_web_path(rel)
        if not target:
            return False
        content_type = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send_bytes(target.read_bytes(), content_type)
        return True

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("请求体不是合法 JSON。") from exc

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_html_file()
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if self._serve_static(rel):
                return
            self._send_json({"error": "未找到文件。"}, status=404)
            return
        if path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            return
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        if path == "/api/projects":
            self._send_json(dashboard_payload())
            return
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            self._send_json(dashboard_payload(unquote(match.group(1))))
            return
        match = re.fullmatch(r"/api/tasks/(\d+)", path)
        if match:
            try:
                self._send_json(task_detail_payload(int(match.group(1))))
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=404)
            return
        # Session endpoints
        match = re.fullmatch(r"/api/sessions", path)
        if match:
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            proj = qs.get("project", [""])[0]
            try:
                self._send_json(list_sessions_action(proj))
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        match = re.fullmatch(r"/api/sessions/(\d+)", path)
        if match:
            try:
                self._send_json(get_session_action(int(match.group(1))))
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=404)
            return
        self._send_json({"error": "未找到页面。"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/goal":
                body = self._read_json_body()
                self._send_json(
                    submit_goal_action(
                        body.get("project") or "",
                        body.get("text") or "",
                        category=body.get("category") or "auto",
                    )
                )
                return
            if path == "/api/tasks":
                body = self._read_json_body()
                self._send_json(
                    create_task_action(
                        body.get("project") or "",
                        body.get("title") or "",
                        content=body.get("content") or "",
                        priority=body.get("priority") or "P2",
                        agent=body.get("agent") or None,
                        max_retries=int(body.get("max_retries") or 3),
                    )
                )
                return
            if path == "/api/requirements":
                body = self._read_json_body()
                self._send_json(
                    submit_requirement_action(
                        body.get("project") or "",
                        body.get("title") or "",
                        execute=bool(body.get("execute", True)),
                        planner=body.get("planner") or "codex",
                        agent=None if body.get("agent") in {"", None, "auto"} else body.get("agent"),
                        priority=body.get("priority") or "P2",
                        max_tasks=int(body.get("max_tasks") or 5),
                        executor=body.get("executor") or "auto",
                        auto_commit=bool(body.get("auto_commit", False)),
                        max_retries=int(body.get("max_retries") or 3),
                        run_async=bool(body.get("run_async", True)),
                    )
                )
                return
            match = re.fullmatch(r"/api/tasks/(\d+)/(retry|stop|promote)", path)
            if match:
                task_id = int(match.group(1))
                action = match.group(2)
                if action == "retry":
                    payload = retry_task_action(task_id)
                elif action == "stop":
                    payload = stop_task_action(task_id)
                else:
                    payload = promote_task_action(task_id)
                self._send_json(payload)
                return
            # Session POST endpoints
            if path == "/api/sessions":
                body = self._read_json_body()
                self._send_json(
                    create_session_action(
                        body.get("project") or "",
                        title=body.get("title") or "",
                    )
                )
                return
            match = re.fullmatch(r"/api/sessions/(\d+)/messages", path)
            if match:
                body = self._read_json_body()
                self._send_json(
                    send_session_message_action(
                        int(match.group(1)),
                        body.get("text") or "",
                        category=body.get("category") or "auto",
                    )
                )
                return
        except (RuntimeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json({"error": "未找到接口。"}, status=404)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            match = re.fullmatch(r"/api/sessions/(\d+)", path)
            if match:
                self._send_json(delete_session_action(int(match.group(1))))
                return
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json({"error": "未找到接口。"}, status=404)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def start_ui_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = True,
    browser_opener: Callable[[str], bool] | None = None,
) -> ThreadingHTTPServer:
    db.init_db()
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    if open_browser:
        opener = browser_opener or webbrowser.open
        threading.Timer(0.3, lambda: opener(f"http://{host}:{port}/")).start()
    return server
