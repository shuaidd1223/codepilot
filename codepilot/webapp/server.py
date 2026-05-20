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
imports (``from codepilot.webapp.server import submit_requirement_action``,
``from codepilot.webapp.server import dashboard_payload``, etc.) remain valid.
"""

from __future__ import annotations

import json
import os
import pkgutil
import re
import sys
import threading
import webbrowser
from importlib import resources
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import ParseResult, parse_qs, unquote, urlparse

import tomllib
from codepilot.core.config import find_config, load_config
from codepilot.storage import database as db
# Re-exported so tests that monkeypatch ``webui_mod.run_requirement_workflow``
# drive :func:`submit_requirement_action` end-to-end.
from codepilot.commands.auto import run_requirement_workflow  # noqa: F401 (re-export)
from codepilot.webapp.actions import (  # noqa: F401 (re-export)
    _GOAL_MAX_BYTES,
    _MAX_EVENTS,
    _MAX_JOB_LOG_LINES,
    _append_event,
    _job_result_summary,
    _next_job_id,
    _update_job,
    cancel_job_action,
    create_project_action,
    create_session_action,
    create_task_action,
    batch_task_action,
    delete_task_action,
    delete_project_action,
    delete_session_action,
    get_session_action,
    list_sessions_action,
    get_task_template_schema_action,
    import_tasks_action,
    list_ui_events,
    list_ui_jobs,
    archive_task_action,
    cancel_task_action,
    promote_task_action,
    project_service_action,
    retry_task_action,
    retry_job_action,
    send_session_message_action,
    stop_session_run_action,
    split_task_action,
    stop_task_action,
    submit_goal_action,
    submit_requirement_action,
    update_project_permission_action,
)
from codepilot.webapp.payloads import (  # noqa: F401 (re-export)
    STATUS_ORDER,
    _compose_log_text,
    _now_iso,
    _parse_depends,
    _read_text,
    _sorted_tasks,
    _tail_text,
    _task_payload,
    ai_status_payload,
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
_UI_STARTED_AT = _now_iso()


# ── Static web assets ────────────────────────────────────────────────────────
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _web_asset_parts(rel: str) -> tuple[str, ...]:
    normalized = str(rel or "").replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise FileNotFoundError(rel)
    return parts


def _load_web_file(name: str) -> bytes:
    parts = _web_asset_parts(name)
    try:
        packaged = pkgutil.get_data("codepilot", "/".join(("web", *parts)))
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        packaged = None
    if packaged is not None:
        return packaged
    try:
        return resources.files("codepilot").joinpath("web", *parts).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass
    for base in _candidate_web_dirs():
        try:
            candidate = base.joinpath(*parts).resolve()
            candidate.relative_to(base.resolve())
        except (ValueError, OSError):
            continue
        if candidate.is_file():
            return candidate.read_bytes()
    raise FileNotFoundError(name)


def _candidate_web_dirs() -> list[Path]:
    candidates = [_WEB_DIR]
    executable = getattr(sys, "executable", "")
    if executable:
        executable_dir = Path(executable).resolve().parent
        candidates.extend([executable_dir / "web", executable_dir / "codepilot" / "web"])
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        candidates.append(Path(bundle_root).resolve() / "codepilot" / "web")
    return candidates


def _safe_web_path(rel: str) -> Path | None:
    """Resolve *rel* under _WEB_DIR, preventing path traversal."""
    try:
        parts = _web_asset_parts(rel)
    except FileNotFoundError:
        return None
    try:
        candidate = _WEB_DIR.joinpath(*parts).resolve()
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


# ── File upload / search helpers ──────────────────────────────────────────────

_CODEPILOT_DATA = Path.home() / ".codepilot" / "data"


def _append_file_refs_to_text(text: str, file_refs: list[dict]) -> str:
    """将附件文件引用展开为内联内容追加到消息文本中。

    支持两种模式：
    - ``data`` 字段包含 base64 数据（前端直接上传）
    - ``path`` 字段包含项目相对路径（@ 引用项目文件）
    """
    import base64

    parts = [text]
    for ref in file_refs:
        name = str(ref.get("name") or "file")

        # 模式 A：直接有 base64 data（前端上传）
        data_b64 = ref.get("data")
        if data_b64:
            try:
                raw = base64.b64decode(data_b64)
                is_text = True
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    is_text = False
                if is_text:
                    parts.append(f"\n\n--- 上传文件: {name} ---\n{raw.decode('utf-8')}\n--- 文件结束 ---")
                else:
                    parts.append(f"\n[已上传文件: {name} ({len(raw)} 字节)]")
                continue
            except Exception as exc:
                parts.append(f"\n[上传文件解析失败: {name} - {exc}]")
                continue

        # 模式 B：通过文件路径引用（@ 引用项目文件）
        path_str = str(ref.get("path") or ref.get("name") or "")
        file_path = Path(path_str)
        if not file_path.is_absolute():
            project_name = ref.get("project") or ""
            project = db.get_project(project_name) if project_name else None
            if project:
                file_path = Path(project["path"]).resolve() / path_str
        try:
            content = file_path.read_bytes()
            is_text = True
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                is_text = False
            if is_text:
                parts.append(f"\n\n--- 文件: {path_str} ---\n{content.decode('utf-8')}\n--- 文件结束 ---")
            else:
                parts.append(f"\n[文件引用: {path_str} ({len(content)} 字节，非文本)]")
        except Exception as exc:
            parts.append(f"\n[文件读取失败: {path_str} - {exc}]")
    return "\n".join(parts)


def _project_upload_dir(project_name: str) -> Path:
    """返回项目上传目录，自动创建。"""
    project = db.get_project(project_name)
    root = Path(project["path"]).resolve() if project else _CODEPILOT_DATA / project_name
    upload_dir = root / ".codepilot" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


# 默认排除目录
_IGNORE_DIRS = frozenset({
    ".git", "node_modules", ".next", ".codepilot", "__pycache__",
    ".venv", "venv", "env", "dist", "build", ".workbuddy",
    ".claude", "target", "bin", "obj", ".tox", ".ruff_cache",
    ".mypy_cache", ".pytest_cache", ".coverage", "htmlcov",
})


@lru_cache(maxsize=32)
def _get_project_files_cached(project_path: str) -> tuple[tuple[str, str], ...]:
    """带缓存的项目文件列表扫描（按修改时间倒序）。

    返回 (path, name) 元组组成的元组，便于缓存哈希。
    缓存 key 是项目路径，最多缓存 32 个项目的文件列表。
    """
    root = Path(project_path).resolve()
    files: list[tuple[str, str]] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(root)
            parts = rel.parts
            if parts and parts[0] in _IGNORE_DIRS:
                continue
            if any(p.startswith(".") for p in parts[:-1]):
                continue
            files.append((str(rel.as_posix()), f.name, f.stat().st_mtime))
        except (OSError, ValueError):
            continue
    # 按修改时间倒序排序
    files.sort(key=lambda x: x[2], reverse=True)
    # 返回时去掉时间戳以节省缓存空间
    return tuple((path, name) for path, name, _ in files)


def _search_project_files(project_name: str, query: str) -> dict:
    """搜索项目下匹配的文件路径，用于 @ 文件引用。"""
    project = db.get_project(project_name)
    if not project:
        return {"files": [], "error": f"项目 '{project_name}' 不存在"}
    root = Path(project["path"]).resolve()

    # 使用缓存的文件列表
    all_files = _get_project_files_cached(str(root))

    results: list[dict] = []
    if not query:
        # 无搜索词时返回最近修改的前 30 个文件
        for path, name in all_files:
            results.append({"path": path, "name": name})
            if len(results) >= 30:
                break
        return {"files": results}

    # 有搜索词时模糊匹配
    q = query.lower()
    for path, name in all_files:
        path_lower = path.lower()
        if q in path_lower or q in name.lower():
            results.append({"path": path, "name": name})
            if len(results) >= 20:
                break
    return {"files": results}


def _save_uploaded_file(project_name: str, filename: str, data: bytes) -> dict:
    """保存上传文件到项目 uploads 目录，返回文件信息。"""
    upload_dir = _project_upload_dir(project_name)
    # 防止路径穿越
    safe_name = Path(filename).name
    if not safe_name:
        safe_name = "unnamed"
    dest = upload_dir / safe_name
    # 同名文件加序号
    counter = 1
    while dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        dest = upload_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    dest.write_bytes(data)
    return {
        "id": safe_name,
        "name": safe_name,
        "path": str(dest.relative_to(Path(project_name).parent) if dest.parent == upload_dir.parent else dest),
        "size": len(data),
        "url": f"/api/files/{project_name}/{dest.name}",
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodePilotUI/0.2"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return

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
        try:
            body = _load_web_file(rel)
        except FileNotFoundError:
            return False
        suffix = PurePosixPath(str(rel or "").replace("\\", "/")).suffix.lower()
        content_type = _CONTENT_TYPES.get(suffix, "application/octet-stream")
        self._send_bytes(body, content_type)
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

    def _handle_post_internal_event(self, body: dict) -> dict:
        remote_host = str((self.client_address or ("", 0))[0] or "")
        if remote_host not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("internal event endpoint only accepts local requests")
        stage = str(body.get("stage") or "").strip()
        if not stage:
            raise RuntimeError("event.stage is required")
        message = str(body.get("message") or "")
        level = str(body.get("level") or "info")
        event_type = str(body.get("type") or "").strip() or None
        extra = body.get("extra") if isinstance(body.get("extra"), dict) else {}
        try:
            task_id = int(body.get("task_id")) if body.get("task_id") is not None else None
        except (ValueError, TypeError):
            # task_id 无法转换为整数时使用 None
            task_id = None

        from codepilot.core import progress_bus

        progress_bus.emit(
            stage=stage,
            message=message,
            task_id=task_id,
            level=level,
            event_type=event_type,
            extra=extra,
        )
        return {"ok": True}

    def _stream_progress_events(self) -> None:
        """GET /api/events/stream — SSE endpoint backed by :mod:`progress_bus`.

        The client opens one long-lived connection; every event published via
        ``progress_bus.emit`` is forwarded as a ``data: {...}\\n\\n`` SSE
        frame. Frames include ``id: <event_id>`` when available so reconnects
        can resume from ``Last-Event-ID`` without losing progress. Heartbeats
        are sent every 15s so intermediate proxies keep the connection alive;
        disconnects tear down the subscription.

        Task state changes from independent processes are posted to
        ``/internal/events`` and then relayed through this same stream. Daemon
        health is piggy-backed onto the stream with a lightweight heartbeat
        check, independent from task state delivery.
        """
        from collections import deque
        import time
        from codepilot.core import progress_bus

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        health_project = (query.get("project") or [""])[0].strip() or None
        last_event_raw = (
            (self.headers.get("Last-Event-ID") or "").strip()
            or (query.get("last_event_id") or [""])[0].strip()
        )
        has_last_event_id = bool(last_event_raw)
        try:
            last_event_id = max(0, int(last_event_raw or "0"))
        except ValueError:
            last_event_id = 0

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

        event_queue_limit = 2048
        event_queue: "deque[dict]" = deque()
        event_queue_cv = threading.Condition()

        def _is_priority_event(event: dict) -> bool:
            extra = (event or {}).get("extra") or {}
            if extra.get("task_log_stream"):
                return False
            return True

        def _enqueue_event(event: dict) -> None:
            incoming = dict(event or {})
            incoming_priority = _is_priority_event(incoming)
            with event_queue_cv:
                if len(event_queue) >= event_queue_limit:
                    if not incoming_priority:
                        # Under load, keep existing key events and drop noisy
                        # task-log micro-chunks first.
                        return
                    drop_idx = None
                    for idx, queued in enumerate(event_queue):
                        if not _is_priority_event(queued):
                            drop_idx = idx
                            break
                    if drop_idx is None:
                        event_queue.popleft()
                    else:
                        del event_queue[drop_idx]
                event_queue.append(incoming)
                event_queue_cv.notify()

        def _write_event_frame(event: dict) -> bool:
            try:
                event_id = int((event or {}).get("id") or 0)
            except (TypeError, ValueError):
                event_id = 0
            frame = ""
            if event_id > 0:
                frame += f"id: {event_id}\n"
            frame += f"data: {json.dumps(event or {}, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

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

        if has_last_event_id:
            token, replay_events = progress_bus.subscribe_with_backlog(_enqueue_event, after_id=last_event_id)
        else:
            token = progress_bus.subscribe(_enqueue_event)
            replay_events = []
        last_keepalive = time.monotonic()
        last_health_check = 0.0
        last_health_key: tuple | None = None
        try:
            # Replay buffered progress only when the client reconnects with a
            # last-seen event id. A fresh page load must not re-toast old task
            # state changes from the in-memory progress history.
            for replay in replay_events:
                if not _write_event_frame(replay):
                    return
                last_keepalive = time.monotonic()

            # Prime the stream with current daemon health so the client has
            # something to render before any progress event arrives.
            initial_health = _health_event()
            last_health_key = _health_key(initial_health)
            if not _write_event_frame(initial_health):
                return

            while True:
                event = None
                with event_queue_cv:
                    # Short wait — lets the daemon-health checker below run
                    # even when no progress events are flowing, so the UI
                    # sees state changes within ~2 seconds.
                    if not event_queue:
                        event_queue_cv.wait(timeout=2.0)
                    if event_queue:
                        event = event_queue.popleft()
                if event is not None:
                    if not _write_event_frame(event):
                        break
                    last_keepalive = time.monotonic()

                now = time.monotonic()
                # Push daemon-health event whenever the state actually
                # changed (alive → stale, stopped → running, etc.).
                if now - last_health_check >= 2.0:
                    last_health_check = now
                    health = _health_event()
                    key = _health_key(health)
                    if key != last_health_key:
                        last_health_key = key
                        if not _write_event_frame(health):
                            break
                        last_keepalive = now

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

    @staticmethod
    def _query_value(parsed: ParseResult, name: str, default: str = "") -> str:
        query = parse_qs(parsed.query)
        return (query.get(name) or [default])[0]

    @classmethod
    def _query_project(cls, parsed: ParseResult) -> str | None:
        return cls._query_value(parsed, "project").strip() or None

    @classmethod
    def _query_bool(cls, parsed: ParseResult, name: str, default: str = "0") -> bool:
        return cls._query_value(parsed, name, default).strip().lower() in {"1", "true", "yes"}

    @classmethod
    def _query_int(cls, parsed: ParseResult, name: str, default: int) -> int:
        try:
            return int(cls._query_value(parsed, name, str(default)) or str(default))
        except ValueError:
            return default

    def _handle_get_health(self, _parsed: ParseResult) -> None:
        self._send_json({"ok": True})

    def _handle_get_daemon_health(self, parsed: ParseResult) -> None:
        self._send_json(daemon_health_payload(self._query_project(parsed)))

    def _handle_get_ai_status(self, parsed: ParseResult) -> None:
        self._send_json(
            ai_status_payload(
                self._query_project(parsed),
                refresh_balance=self._query_bool(parsed, "refresh"),
            )
        )

    def _handle_get_event_stream(self, _parsed: ParseResult) -> None:
        # Server-sent events: push live progress to the dashboard so the
        # user sees builder/reviewer output in real time instead of polling.
        self._stream_progress_events()

    def _handle_get_projects(self, _parsed: ParseResult) -> None:
        self._send_json(dashboard_payload())

    def _handle_get_task_template(self, _parsed: ParseResult) -> None:
        self._send_json(get_task_template_schema_action())

    def _handle_get_sessions(self, parsed: ParseResult) -> None:
        qs = parse_qs(parsed.query)
        proj = qs.get("project", [""])[0]
        query = qs.get("q", [""])[0]
        limit = self._query_int(parsed, "limit", 50)
        try:
            self._send_json(list_sessions_action(proj, query=query, limit=limit))
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=400)

    def _dispatch_get_asset(self, path: str) -> bool:
        if path == "/":
            self._send_html_file()
            return True
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if self._serve_static(rel):
                return True
            self._send_json({"error": "未找到文件。"}, status=404)
            return True
        if path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            return True
        return False

    def _dispatch_get_exact(self, path: str, parsed: ParseResult) -> bool:
        handlers: dict[str, Callable[[ParseResult], None]] = {
            "/api/health": self._handle_get_health,
            "/api/daemon/health": self._handle_get_daemon_health,
            "/api/ai/status": self._handle_get_ai_status,
            "/api/events/stream": self._handle_get_event_stream,
            "/api/projects": self._handle_get_projects,
            "/api/task-template": self._handle_get_task_template,
            "/api/sessions": self._handle_get_sessions,
        }
        handler = handlers.get(path)
        if not handler:
            return False
        handler(parsed)
        return True

    def _dispatch_get_project_detail(self, path: str) -> bool:
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if not match:
            return False
        self._send_json(dashboard_payload(unquote(match.group(1))))
        return True

    def _dispatch_get_task_detail(self, path: str) -> bool:
        match = re.fullmatch(r"/api/tasks/(\d+)", path)
        if not match:
            return False
        try:
            self._send_json(task_detail_payload(int(match.group(1))))
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=404)
        return True

    def _dispatch_get_task_log(self, path: str, parsed: ParseResult) -> bool:
        match = re.fullmatch(r"/api/tasks/(\d+)/log", path)
        if not match:
            return False
        try:
            self._send_json(
                task_log_delta(
                    int(match.group(1)),
                    offset=self._query_int(parsed, "offset", 0),
                )
            )
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=404)
        return True

    def _dispatch_get_session_detail(self, path: str) -> bool:
        match = re.fullmatch(r"/api/sessions/(\d+)", path)
        if not match:
            return False
        try:
            self._send_json(get_session_action(int(match.group(1))))
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=404)
        return True

    def _dispatch_get_file_search(self, path: str, parsed: ParseResult) -> bool:
        """搜索项目文件（用于 @ 文件引用）。"""
        match = re.fullmatch(r"/api/projects/([^/]+)/files/search", path)
        if not match:
            return False
        project_name = unquote(match.group(1))
        query = (parse_qs(parsed.query).get("q") or [""])[0].strip()
        self._send_json(_search_project_files(project_name, query))
        return True

    def _dispatch_get_uploaded_file(self, path: str) -> bool:
        """提供已上传文件的访问。"""
        match = re.fullmatch(r"/api/files/([^/]+)/(.+)", path)
        if not match:
            return False
        project_name = unquote(match.group(1))
        filename = unquote(match.group(2))
        upload_dir = _project_upload_dir(project_name)
        file_path = (upload_dir / filename).resolve()
        # 防止路径穿越
        try:
            file_path.relative_to(upload_dir.resolve())
        except ValueError:
            self._send_json({"error": "非法文件路径"}, status=400)
            return True
        if not file_path.is_file():
            self._send_json({"error": "文件不存在"}, status=404)
            return True
        content_type = _CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
        self._send_bytes(file_path.read_bytes(), content_type)
        return True

    def _dispatch_get_pattern(self, path: str, parsed: ParseResult) -> bool:
        if self._dispatch_get_project_detail(path):
            return True
        if self._dispatch_get_task_detail(path):
            return True
        if self._dispatch_get_task_log(path, parsed):
            return True
        if self._dispatch_get_file_search(path, parsed):
            return True
        if self._dispatch_get_uploaded_file(path):
            return True
        return self._dispatch_get_session_detail(path)

    def _dispatch_get(self, parsed: ParseResult) -> bool:
        path = parsed.path
        if self._dispatch_get_asset(path):
            return True
        if self._dispatch_get_exact(path, parsed):
            return True
        return self._dispatch_get_pattern(path, parsed)

    def do_GET(self) -> None:  # noqa: N802
        if self._dispatch_get(urlparse(self.path)):
            return
        self._send_json({"error": "未找到页面。"}, status=404)

    def _handle_post_goal(self, body: dict) -> dict:
        return submit_goal_action(
            body.get("project") or "",
            body.get("text") or "",
            category=body.get("category") or "auto",
            qa_history=body.get("qa_history") if isinstance(body.get("qa_history"), list) else [],
            original_title=(body.get("original_title") or "").strip(),
            clarify_answers=body.get("clarify_answers") if isinstance(body.get("clarify_answers"), list) else [],
            clarify_questions=body.get("clarify_questions") if isinstance(body.get("clarify_questions"), list) else [],
        )

    def _handle_post_projects(self, body: dict) -> dict:
        return create_project_action(
            body.get("path") or "",
            name=body.get("name") or "",
            no_config=bool(body.get("no_config", False)),
        )

    def _handle_post_tasks(self, body: dict) -> dict:
        return create_task_action(
            body.get("project") or "",
            body.get("title") or "",
            content=body.get("content") or "",
            priority=body.get("priority") or "P2",
            agent=body.get("agent") or None,
            max_retries=int(body.get("max_retries") or 3),
            mode=body.get("mode") or "full",
        )

    def _handle_post_tasks_batch(self, body: dict) -> dict:
        return batch_task_action(
            body.get("task_ids") if isinstance(body.get("task_ids"), list) else [],
            body.get("action") or "",
            message=body.get("message") or "",
        )

    def _handle_post_tasks_import(self, body: dict) -> dict:
        return import_tasks_action(
            body.get("project") or "",
            body.get("items") if isinstance(body.get("items"), list) else [],
        )

    def _handle_post_requirements(self, body: dict) -> dict:
        return submit_requirement_action(
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
            qa_history=body.get("qa_history") if isinstance(body.get("qa_history"), list) else [],
            original_title=(body.get("original_title") or "").strip(),
            clarify_answers=body.get("clarify_answers") if isinstance(body.get("clarify_answers"), list) else [],
            clarify_questions=body.get("clarify_questions") if isinstance(body.get("clarify_questions"), list) else [],
        )

    def _handle_post_sessions(self, body: dict) -> dict:
        return create_session_action(
            body.get("project") or "",
            title=body.get("title") or "",
        )

    def _handle_post_file_upload(self, body: dict) -> dict:
        """处理文件上传（base64 编码）。"""
        project = str(body.get("project") or "")
        filename = str(body.get("name") or "unnamed")
        data_b64 = str(body.get("data") or "")
        if not project or not data_b64:
            raise RuntimeError("缺少 project 或 data 参数。")
        import base64
        try:
            data = base64.b64decode(data_b64)
        except Exception as exc:
            raise RuntimeError(f"base64 解码失败：{exc}") from exc
        return _save_uploaded_file(project, filename, data)

    def _dispatch_post_exact(self, path: str, get_body: Callable[[], dict]) -> dict | None:
        """Handle POST endpoints with exact paths; return ``None`` if unmatched."""
        handlers: dict[str, Callable[[dict], dict]] = {
            "/internal/events": self._handle_post_internal_event,
            "/api/goal": self._handle_post_goal,
            "/api/projects": self._handle_post_projects,
            "/api/tasks": self._handle_post_tasks,
            "/api/tasks/batch": self._handle_post_tasks_batch,
            "/api/tasks/import": self._handle_post_tasks_import,
            "/api/requirements": self._handle_post_requirements,
            "/api/sessions": self._handle_post_sessions,
            "/api/files/upload": self._handle_post_file_upload,
        }
        handler = handlers.get(path)
        if not handler:
            return None
        return handler(get_body())

    def _dispatch_post_task_action(self, path: str) -> dict | None:
        match = re.fullmatch(r"/api/tasks/(\d+)/(retry|stop|promote|split|cancel|archive|delete)", path)
        if not match:
            return None
        task_id = int(match.group(1))
        action = match.group(2)
        action_handlers: dict[str, Callable[[int], dict]] = {
            "retry": retry_task_action,
            "stop": stop_task_action,
            "promote": promote_task_action,
            "split": split_task_action,
            "cancel": cancel_task_action,
            "archive": archive_task_action,
            "delete": delete_task_action,
        }
        return action_handlers[action](task_id)

    def _dispatch_post_job_action(self, path: str) -> dict | None:
        match = re.fullmatch(r"/api/jobs/(\d+)/(cancel|retry)", path)
        if not match:
            return None
        job_id = int(match.group(1))
        action = match.group(2)
        action_handlers: dict[str, Callable[[int], dict]] = {
            "cancel": cancel_job_action,
            "retry": retry_job_action,
        }
        return action_handlers[action](job_id)

    def _dispatch_post_project_service(self, path: str) -> dict | None:
        match = re.fullmatch(r"/api/projects/([^/]+)/(tasks|inspect)/(start|stop|status)", path)
        if not match:
            return None
        return project_service_action(
            unquote(match.group(1)),
            match.group(2),
            match.group(3),
        )

    def _dispatch_post_session_message(self, path: str, get_body: Callable[[], dict]) -> dict | None:
        match = re.fullmatch(r"/api/sessions/(\d+)/messages", path)
        if not match:
            return None
        body = get_body()
        text = str(body.get("text") or "")

        # 如果有附件文件引用，读取文件内容追加到消息文本中
        file_refs = body.get("files")
        if isinstance(file_refs, list) and file_refs:
            text = _append_file_refs_to_text(text, file_refs)

        kwargs = {
            "category": body.get("category") or "auto",
            "clarify_answers": body.get("clarify_answers") if isinstance(body.get("clarify_answers"), list) else [],
            "run_async": bool(body.get("run_async", True)),
        }
        if isinstance(body.get("runtime"), dict):
            kwargs["runtime_config"] = body.get("runtime")
        return send_session_message_action(int(match.group(1)), text, **kwargs)

    def _dispatch_post_session_run_stop(self, path: str) -> dict | None:
        match = re.fullmatch(r"/api/sessions/(\d+)/runs/(\d+)/stop", path)
        if not match:
            return None
        return stop_session_run_action(int(match.group(1)), int(match.group(2)))

    def _dispatch_post_project_permission(self, path: str, get_body: Callable[[], dict]) -> dict | None:
        match = re.fullmatch(r"/api/projects/([^/]+)/permission", path)
        if not match:
            return None
        body = get_body()
        return update_project_permission_action(
            unquote(match.group(1)),
            mode=str(body.get("mode") or "").strip(),
        )

    def _dispatch_post_pattern(self, path: str, get_body: Callable[[], dict]) -> dict | None:
        """Handle regex POST routes; return ``None`` if unmatched."""
        payload = self._dispatch_post_task_action(path)
        if payload is not None:
            return payload
        payload = self._dispatch_post_job_action(path)
        if payload is not None:
            return payload
        payload = self._dispatch_post_project_service(path)
        if payload is not None:
            return payload
        payload = self._dispatch_post_session_run_stop(path)
        if payload is not None:
            return payload
        payload = self._dispatch_post_project_permission(path, get_body)
        if payload is not None:
            return payload
        return self._dispatch_post_session_message(path, get_body)

    def _dispatch_post(self, path: str, get_body: Callable[[], dict]) -> dict | None:
        payload = self._dispatch_post_exact(path, get_body)
        if payload is not None:
            return payload
        return self._dispatch_post_pattern(path, get_body)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body_cache: dict | None = None

        def _body() -> dict:
            nonlocal body_cache
            if body_cache is None:
                body_cache = self._read_json_body()
            return body_cache

        try:
            payload = self._dispatch_post(path, _body)
            if payload is None:
                self._send_json({"error": "未找到接口。"}, status=404)
                return
            self._send_json(payload)
            return
        except (RuntimeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return

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
    bound_host, bound_port = server.server_address
    try:
        db.upsert_service_state(
            "webui",
            "_global",
            pid=os.getpid(),
            status="running",
            log_path="",
            heartbeat_at=_now_iso(),
            meta={
                "pid": os.getpid(),
                "host": str(bound_host or host),
                "port": int(bound_port),
                "started_at": _now_iso(),
            },
        )
    except Exception:  # noqa: BLE001
        # 记录服务状态失败不应阻止服务器启动
        pass
    if open_browser:
        opener = browser_opener or webbrowser.open
        threading.Timer(0.3, lambda: opener(f"http://{bound_host}:{bound_port}/")).start()
    return server
