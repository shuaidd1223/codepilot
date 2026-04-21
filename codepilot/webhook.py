"""Webhook 通知：任务状态变更时推送到飞书/企微/通用 HTTP."""

from __future__ import annotations

import json
import os
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from codepilot import db
from codepilot.config import load_config


_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


def _normalize_priority(value: object) -> str:
    priority = str(value or "P2").strip().upper()
    if priority not in {"P0", "P1", "P2", "P3"}:
        raise RuntimeError("priority 只支持 P0 / P1 / P2 / P3。")
    return priority


def _normalize_max_retries(value: object) -> int:
    try:
        max_retries = int(value if value is not None else 3)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("max_retries 必须是整数。") from exc
    return max(0, max_retries)


def create_webhook_task(payload: dict[str, Any]) -> dict:
    """Create one backlog task from a webhook POST payload."""
    db.init_db()

    project = str(payload.get("project") or "").strip()
    if not project:
        raise RuntimeError("project 不能为空。")
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    title = " ".join(str(payload.get("title") or "").split())
    if not title:
        raise RuntimeError("title 不能为空。")

    content = payload.get("content")
    if content is None:
        content = payload.get("body")
    if content is None:
        content = payload.get("description")

    task = db.create_task(
        project=project,
        title=title,
        content=str(content or ""),
        agent=str(payload.get("agent") or project_info.get("default_mode") or "dual").strip() or "dual",
        priority=_normalize_priority(payload.get("priority")),
        depends_on=payload.get("depends_on") or payload.get("depends"),
        project_path=project_info["path"],
        max_retries=_normalize_max_retries(payload.get("max_retries")),
        source="webhook",
    )
    return {"ok": True, "task": task, "message": f"任务 #{task['id']} 已创建。"}


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "CodePilotWebhook/0.1"

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise RuntimeError("Content-Length 不合法。") from exc
        if length > _MAX_WEBHOOK_BODY_BYTES:
            raise RuntimeError("请求体过大。")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("请求体不是合法 JSON。") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("请求体必须是 JSON 对象。")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json({"ok": True, "status": "ok", "service": "webhook"})
            return
        self._send_json({"error": "未找到接口。"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/tasks":
            self._send_json({"error": "未找到接口。"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json_body()
            self._send_json(create_webhook_task(payload), status=HTTPStatus.CREATED)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def start_webhook_server(*, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Start the local webhook HTTP server."""
    db.init_db()
    return ThreadingHTTPServer((host, port), WebhookHandler)


def _get_webhook_config(project_path: str) -> dict:
    """Read webhook settings from config plus environment overrides."""
    config = {"webhook_url": "", "enabled": False}
    toml_path = Path(project_path) / "AGENTS.toml"
    cfg = load_config(toml_path) if toml_path.exists() else None
    if cfg:
        config["webhook_url"] = cfg.webhook_url or cfg.notifications.get("webhook_url", "")
        config["enabled"] = bool(cfg.notifications_enabled or cfg.notifications.get("enabled", False))

    if os.environ.get("CODEPILOT_WEBHOOK_URL"):
        config["webhook_url"] = os.environ["CODEPILOT_WEBHOOK_URL"]
        config["enabled"] = True
    if os.environ.get("CODEPILOT_WEBHOOK_ENABLED"):
        config["enabled"] = os.environ["CODEPILOT_WEBHOOK_ENABLED"].lower() in ("true", "1", "yes")

    return config


def _send_feishu_webhook(url: str, text: str) -> bool:
    """发送飞书/企微机器人消息."""
    payload = json.dumps({
        "msg_type": "text",
        "content": {"text": text},
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("code", 0) == 0
    except Exception:
        return False


def _send_generic_webhook(url: str, payload: dict) -> bool:
    """发送通用 HTTP POST（JSON）."""
    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def notify_task_status(
    project_path: str,
    task_id: int,
    task_title: str,
    status: str,
    error_message: str = "",
) -> bool:
    """
    发送任务状态变更通知.

    支持飞书/企微机器人 webhook 和通用 HTTP webhook。
    返回是否发送成功。
    """
    config = _get_webhook_config(project_path)
    if not config.get("enabled") or not config.get("webhook_url"):
        return False

    url = config["webhook_url"]

    # 构建通知文本
    if status == "done":
        emoji = "[OK]"
        status_text = "已完成"
    elif status == "failed":
        emoji = "[X]"
        status_text = "失败"
    elif status == "in_progress":
        emoji = "[>>]"
        status_text = "开始执行"
    else:
        emoji = "[--]"
        status_text = status

    project_name = Path(project_path).name
    message = (
        f"{emoji} CodePilot #{task_id} {status_text}\n"
        f"项目: {project_name}\n"
        f"任务: {task_title}"
    )
    if error_message:
        message += f"\n错误: {error_message[:100]}"

    # 检测 webhook 类型
    if "feishu" in url or "lark" in url or "wecom" in url or "qyapi" in url:
        ok = _send_feishu_webhook(url, message)
    else:
        ok = _send_generic_webhook(url, {
            "event": "task_status_changed",
            "task_id": task_id,
            "task_title": task_title,
            "status": status,
            "project": project_name,
            "error_message": error_message,
        })

    # Also fire a best-effort desktop toast so the user doesn't need to look
    # at the terminal to know a long task finished. Failures here never
    # affect the webhook success return.
    if status in {"done", "failed"}:
        try:
            _send_desktop_notification(
                title=f"CodePilot: 任务 {status_text} (#{task_id})",
                body=f"{project_name} — {task_title}",
            )
        except Exception:
            pass

    return ok


def _send_desktop_notification(*, title: str, body: str) -> bool:
    """Best-effort cross-platform desktop notification.

    Windows: PowerShell BurntToast / Windows.UI.Notifications ToastNotification
             via `msg` command fallback.
    macOS:   osascript.
    Linux:   notify-send.

    Skipped silently when ``CODEPILOT_DESKTOP_NOTIFY`` is set to ``0``, when
    the tool is absent, or when the process has no attached user session.
    """
    import platform
    import shutil
    import subprocess

    if os.environ.get("CODEPILOT_DESKTOP_NOTIFY", "").strip() in {"0", "false", "no"}:
        return False

    title = (title or "CodePilot")[:120]
    body = (body or "")[:500]
    system = platform.system().lower()

    try:
        if system == "darwin":
            script = f'display notification "{body}" with title "{title}"'
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
            return True
        if system == "linux":
            if shutil.which("notify-send"):
                subprocess.run(
                    ["notify-send", title, body],
                    capture_output=True, timeout=3,
                )
                return True
            return False
        if system == "windows":
            # PowerShell one-liner using Windows Runtime ToastNotification.
            # Escape single quotes by doubling them (PowerShell convention).
            ps_title = title.replace("'", "''")
            ps_body = body.replace("'", "''")
            ps_cmd = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
                "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                f"$template.GetElementsByTagName('text').Item(0).AppendChild($template.CreateTextNode('{ps_title}')) > $null;"
                f"$template.GetElementsByTagName('text').Item(1).AppendChild($template.CreateTextNode('{ps_body}')) > $null;"
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($template);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('CodePilot').Show($toast);"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, timeout=5,
            )
            return True
    except Exception:
        return False
    return False
