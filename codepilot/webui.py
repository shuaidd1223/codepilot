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
from urllib.parse import parse_qs, unquote, urlparse

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
    create_project_action,
    create_session_action,
    create_task_action,
    delete_project_action,
    delete_session_action,
    get_session_action,
    list_sessions_action,
    list_ui_events,
    list_ui_jobs,
    promote_task_action,
    project_service_action,
    retry_task_action,
    send_session_message_action,
    split_task_action,
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
    daemon_health_payload,
    dashboard_payload,
    project_summary,
    task_detail_payload,
    task_log_delta,
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

    def _stream_progress_events(self) -> None:
        """GET /api/events/stream — SSE endpoint backed by :mod:`progress_bus`.

        The client opens one long-lived connection; every event published via
        ``progress_bus.emit`` is forwarded as a ``data: {...}\\n\\n`` SSE
        frame. Heartbeats are sent every 15s so intermediate proxies keep
        the connection alive; disconnects tear down the subscription.

        Daemon health is piggy-backed onto the same stream: we poll the
        local heartbeat file every 2s and push a ``stage=daemon-health``
        event whenever the state changes (alive ↔ stale ↔ dead). That way
        the Web UI banner reacts in ~2s without a separate polling timer.
        """
        import queue as _queue
        import time
        from codepilot import progress_bus

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"retry: 5000\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        # Keep a larger in-memory queue so task_log_stream micro-events can
        # flow smoothly during heavy output bursts without starving normal
        # progress events.
        event_queue: "_queue.Queue[dict]" = _queue.Queue(maxsize=2048)

        def _forward(event: dict) -> None:
            try:
                event_queue.put_nowait(event)
            except _queue.Full:
                # If the client can't drain fast enough, drop the oldest
                # event in favour of the newest — better than blocking emit.
                try:
                    event_queue.get_nowait()
                    event_queue.put_nowait(event)
                except Exception:
                    pass

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        health_project = (query.get("project") or [""])[0].strip() or None

        def _health_event() -> dict:
            payload = daemon_health_payload(health_project)
            return {
                "timestamp": _now_iso(),
                "task_id": None,
                "stage": "daemon-health",
                "level": "info" if payload.get("alive") else "warning",
                "message": payload.get("reason") or "daemon ok",
                "extra": payload,
            }

        def _health_key(health: dict) -> tuple:
            """Stable identity of the "health state" — we only re-push on changes."""
            extra = (health or {}).get("extra") or {}
            return (
                bool(extra.get("alive")),
                bool(extra.get("running")),
                int(extra.get("pid") or 0),
                # Bucket stale-seconds to avoid pushing every 2s when numbers tick.
                int((int(extra.get("stale_seconds") or 0)) // 10),
            )

        token = progress_bus.subscribe(_forward)
        last_keepalive = time.monotonic()
        last_health_check = 0.0
        last_health_key: tuple | None = None
        try:
            # Prime the stream with current daemon health so the client has
            # something to render before any progress event arrives.
            initial_health = _health_event()
            last_health_key = _health_key(initial_health)
            try:
                self.wfile.write(f"data: {json.dumps(initial_health, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

            while True:
                try:
                    # Short poll — lets the daemon-health checker below run
                    # even when no progress events are flowing, so the UI
                    # sees state changes within ~2 seconds.
                    event = event_queue.get(timeout=2)
                    frame = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    try:
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                        last_keepalive = time.monotonic()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                except _queue.Empty:
                    pass  # fall through to health / keepalive below

                now = time.monotonic()
                # Push daemon-health event whenever the state actually
                # changed (alive → stale, stopped → running, etc.).
                if now - last_health_check >= 2.0:
                    last_health_check = now
                    health = _health_event()
                    key = _health_key(health)
                    if key != last_health_key:
                        last_health_key = key
                        try:
                            self.wfile.write(
                                f"data: {json.dumps(health, ensure_ascii=False)}\n\n".encode("utf-8")
                            )
                            self.wfile.flush()
                            last_keepalive = now
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            break

                # Keep the connection alive through proxies even when
                # nothing's changing.
                if now - last_keepalive >= 15.0:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_keepalive = now
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
        finally:
            progress_bus.unsubscribe(token)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
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
        if path == "/api/daemon/health":
            query = parse_qs(parsed.query)
            project = (query.get("project") or [""])[0].strip() or None
            self._send_json(daemon_health_payload(project))
            return
        if path == "/api/events/stream":
            # Server-sent events: push live progress to the dashboard so the
            # user sees builder/reviewer output in real time instead of polling.
            self._stream_progress_events()
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
        # Incremental log fetch — frontend tracks its own offset and polls
        # (or refetches on SSE event) to append only the new bytes.
        match = re.fullmatch(r"/api/tasks/(\d+)/log", path)
        if match:
            qs = parse_qs(parsed.query)
            try:
                offset = int((qs.get("offset", ["0"]) or ["0"])[0] or "0")
            except ValueError:
                offset = 0
            try:
                self._send_json(task_log_delta(int(match.group(1)), offset=offset))
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=404)
            return
        # Session endpoints
        match = re.fullmatch(r"/api/sessions", path)
        if match:
            qs = parse_qs(parsed.query)
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
                raw_history = body.get("qa_history") or []
                if not isinstance(raw_history, list):
                    raw_history = []
                self._send_json(
                    submit_goal_action(
                        body.get("project") or "",
                        body.get("text") or "",
                        category=body.get("category") or "auto",
                        qa_history=raw_history,
                        original_title=(body.get("original_title") or "").strip(),
                    )
                )
                return
            if path == "/api/projects":
                body = self._read_json_body()
                self._send_json(
                    create_project_action(
                        body.get("path") or "",
                        name=body.get("name") or "",
                        no_config=bool(body.get("no_config", False)),
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
                raw_history = body.get("qa_history") or []
                if not isinstance(raw_history, list):
                    raw_history = []
                self._send_json(
                    submit_requirement_action(
                        body.get("project") or "",
                        body.get("title") or "",
                        execute=bool(body.get("execute", True)),
                        planner=(body.get("planner") or "").strip() or None,
                        agent=None if body.get("agent") in {"", None, "auto"} else body.get("agent"),
                        priority=body.get("priority") or "P2",
                        max_tasks=int(body.get("max_tasks") or 5),
                        executor=body.get("executor") or "auto",
                        auto_commit=bool(body.get("auto_commit", False)),
                        max_retries=int(body.get("max_retries") or 3),
                        run_async=bool(body.get("run_async", True)),
                        qa_history=raw_history,
                        original_title=(body.get("original_title") or "").strip(),
                    )
                )
                return
            match = re.fullmatch(r"/api/tasks/(\d+)/(retry|stop|promote|split)", path)
            if match:
                task_id = int(match.group(1))
                action = match.group(2)
                if action == "retry":
                    payload = retry_task_action(task_id)
                elif action == "stop":
                    payload = stop_task_action(task_id)
                elif action == "split":
                    payload = split_task_action(task_id)
                else:
                    payload = promote_task_action(task_id)
                self._send_json(payload)
                return
            match = re.fullmatch(r"/api/projects/([^/]+)/(tasks|inspect)/(start|stop|status)", path)
            if match:
                self._send_json(
                    project_service_action(
                        unquote(match.group(1)),
                        match.group(2),
                        match.group(3),
                    )
                )
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
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            match = re.fullmatch(r"/api/projects/([^/]+)", path)
            if match:
                self._send_json(delete_project_action(unquote(match.group(1))))
                return
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
