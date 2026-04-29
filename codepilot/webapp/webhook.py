"""Webhook 通知：任务状态变更时推送到飞书/企微/通用 HTTP."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from codepilot.storage import database as db
from codepilot.core.config import load_config


_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _external_notifications_allowed() -> bool:
    if _truthy_env("CODEPILOT_SUPPRESS_EXTERNAL_NOTIFICATIONS"):
        return False
    if "PYTEST_CURRENT_TEST" in os.environ and not _truthy_env("CODEPILOT_ALLOW_TEST_NOTIFICATIONS"):
        return False
    return True


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
    config = {
        "webhook_url": "",
        "provider": "auto",
        "webhook_secret": "",
        "enabled": False,
    }
    toml_path = Path(project_path) / "AGENTS.toml"
    cfg = load_config(toml_path) if toml_path.exists() else None
    if cfg:
        config["webhook_url"] = cfg.webhook_url or cfg.notifications.get("webhook_url", "")
        config["provider"] = str(
            cfg.webhook_provider
            or cfg.notifications.get("provider", "auto")
            or "auto"
        ).strip().lower() or "auto"
        config["webhook_secret"] = str(
            cfg.webhook_secret
            or cfg.notifications.get("webhook_secret", "")
            or ""
        )
        config["enabled"] = bool(cfg.notifications_enabled or cfg.notifications.get("enabled", False))

    if os.environ.get("CODEPILOT_WEBHOOK_URL"):
        config["webhook_url"] = os.environ["CODEPILOT_WEBHOOK_URL"]
        config["enabled"] = True
    if os.environ.get("CODEPILOT_WEBHOOK_PROVIDER"):
        config["provider"] = os.environ["CODEPILOT_WEBHOOK_PROVIDER"].strip().lower() or "auto"
    if os.environ.get("CODEPILOT_WEBHOOK_SECRET"):
        config["webhook_secret"] = os.environ["CODEPILOT_WEBHOOK_SECRET"]
    if os.environ.get("CODEPILOT_WEBHOOK_ENABLED"):
        config["enabled"] = os.environ["CODEPILOT_WEBHOOK_ENABLED"].lower() in ("true", "1", "yes")

    return config


def _normalize_webhook_provider(value: object) -> str:
    provider = str(value or "auto").strip().lower()
    return provider if provider in {"auto", "feishu", "wecom", "generic"} else "auto"


def _detect_webhook_provider(url: str) -> str:
    lowered = str(url or "").lower()
    if any(marker in lowered for marker in ("open.feishu.cn", "feishu", "lark", "larksuite")):
        return "feishu"
    if any(marker in lowered for marker in ("qyapi.weixin.qq.com", "wecom")):
        return "wecom"
    return "generic"


def _feishu_field(label: str, value: object, *, is_short: bool = True) -> dict[str, Any]:
    return {
        "is_short": is_short,
        "text": {
            "tag": "lark_md",
            "content": f"**{str(label or '').strip()}**\n`{str(value or '-').strip() or '-'}`",
        },
    }


def _feishu_template_for_status(status: str) -> str:
    if status == "done":
        return "green"
    if status == "failed":
        return "red"
    if status == "in_progress":
        return "blue"
    return "grey"


def _task_status_meta(status: str) -> dict[str, str]:
    meta = {
        "done": {
            "icon": "✅",
            "label": "已完成",
            "desktop_icon": "dialog-information",
        },
        "failed": {
            "icon": "❌",
            "label": "失败",
            "desktop_icon": "dialog-error",
        },
        "in_progress": {
            "icon": "🚀",
            "label": "开始执行",
            "desktop_icon": "dialog-information",
        },
        "cancelled": {
            "icon": "⏹️",
            "label": "已取消",
            "desktop_icon": "dialog-warning",
        },
    }
    return meta.get(
        status,
        {
            "icon": "ℹ️",
            "label": status or "状态更新",
            "desktop_icon": "dialog-information",
        },
    )


def _build_feishu_card(
    title: str,
    fields: list[dict[str, Any]],
    *,
    template: str = "blue",
    detail_label: str = "",
    detail: str = "",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if fields:
        elements.append({"tag": "div", "fields": fields})
    detail = str(detail or "").strip()
    if detail:
        label = str(detail_label or "说明").strip()
        elements.extend(
            [
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**{label}**\n{detail[:600]}"}},
            ]
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": str(title or "CodePilot 通知")[:120]},
        },
        "elements": elements,
    }


def _build_feishu_payload(card: dict[str, Any], *, secret: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": card,
    }
    secret = str(secret or "").strip()
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(
            hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
    return payload


def _send_feishu_webhook(url: str, card: dict[str, Any], *, secret: str = "") -> bool:
    """发送飞书机器人卡片消息."""
    if not _external_notifications_allowed():
        return False
    payload = json.dumps(_build_feishu_payload(card, secret=secret), ensure_ascii=False).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if not raw:
                return 200 <= getattr(resp, "status", 200) < 300
            result = json.loads(raw.decode("utf-8"))
            return int(result.get("code", 0) or 0) == 0
    except Exception:
        return False


def _send_wecom_webhook(url: str, text: str) -> bool:
    """发送企业微信机器人消息."""
    if not _external_notifications_allowed():
        return False
    payload = json.dumps({
        "msgtype": "text",
        "text": {"content": text},
    }, ensure_ascii=False).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if not raw:
                return 200 <= getattr(resp, "status", 200) < 300
            result = json.loads(raw.decode("utf-8"))
            return int(result.get("errcode", 0) or 0) == 0
    except Exception:
        return False


def _send_generic_webhook(url: str, payload: dict) -> bool:
    """发送通用 HTTP POST（JSON）."""
    if not _external_notifications_allowed():
        return False
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
    status_meta = _task_status_meta(status)
    icon = status_meta["icon"]
    status_text = status_meta["label"]

    project_name = Path(project_path).name
    message = (
        f"{icon} CodePilot #{task_id} · {status_text}\n"
        f"项目：{project_name}\n"
        f"任务：{task_title}"
    )
    if error_message:
        message += f"\n错误：{error_message[:100]}"

    desktop_ok = False
    if status in {"done", "failed", "cancelled"}:
        try:
            desktop_ok = _send_desktop_notification(
                title=f"{icon} CodePilot #{task_id} · {status_text}",
                body=(
                    f"项目：{project_name}\n"
                    f"任务：{task_title}"
                    + (f"\n错误：{error_message[:160]}" if error_message else "")
                ),
                icon=status_meta["desktop_icon"],
            )
        except Exception:
            desktop_ok = False

    config = _get_webhook_config(project_path)
    if not config.get("enabled") or not config.get("webhook_url"):
        return desktop_ok

    url = config["webhook_url"]

    provider = _normalize_webhook_provider(config.get("provider"))
    if provider == "auto":
        provider = _detect_webhook_provider(url)

    if provider == "feishu":
        card = _build_feishu_card(
            f"{icon} CodePilot #{task_id} · {status_text}",
            [
                _feishu_field("项目", project_name),
                _feishu_field("任务", f"#{task_id}"),
                _feishu_field("状态", status_text),
                _feishu_field("标题", task_title, is_short=False),
            ],
            template=_feishu_template_for_status(status),
            detail_label="错误",
            detail=error_message[:300] if error_message else "",
        )
        ok = _send_feishu_webhook(url, card, secret=str(config.get("webhook_secret") or ""))
    elif provider == "wecom":
        ok = _send_wecom_webhook(url, message)
    else:
        ok = _send_generic_webhook(url, {
            "event": "task_status_changed",
            "task_id": task_id,
            "task_title": task_title,
            "status": status,
            "status_text": status_text,
            "icon": icon,
            "project": project_name,
            "error_message": error_message,
        })

    return bool(ok or desktop_ok)


def notify_task_event(
    project_path: str,
    task_id: int,
    task_title: str,
    *,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    status: str = "",
    summary: str = "",
) -> bool:
    """Send a structured task progress event to the configured external UI webhook."""
    config = _get_webhook_config(project_path)
    if not config.get("enabled") or not config.get("webhook_url"):
        return False

    url = config["webhook_url"]
    project_name = Path(project_path).name
    event_label = {
        "started": "开始执行",
        "phase_start": "阶段开始",
        "phase_end": "阶段完成",
        "phase_retry": "阶段重试",
        "review_pass": "Review 通过",
        "review_fail": "Review 未通过",
        "merged": "分支已合并",
        "merge_failed": "合并失败",
        "requeued": "已回队列",
        "preflight_skip": "预检跳过",
    }.get(event, event or "状态更新")
    phase_text = phase or "-"
    detail = (message or summary or "").strip()
    text = (
        f"[{level.upper()}] CodePilot #{task_id} {event_label}\n"
        f"项目: {project_name}\n"
        f"任务: {task_title}\n"
        f"阶段: {phase_text}"
    )
    if status:
        text += f"\n状态: {status}"
    if detail:
        text += f"\n说明: {detail[:300]}"

    provider = _normalize_webhook_provider(config.get("provider"))
    if provider == "auto":
        provider = _detect_webhook_provider(url)

    if provider == "feishu":
        card = _build_feishu_card(
            f"CodePilot #{task_id} {event_label}",
            [
                _feishu_field("项目", project_name),
                _feishu_field("任务", f"#{task_id}"),
                _feishu_field("事件", event_label),
                _feishu_field("阶段", phase_text),
                _feishu_field("状态", status or "-"),
                _feishu_field("标题", task_title, is_short=False),
            ],
            template="red" if level == "error" or event in {"failed", "merge_failed"} else "blue",
            detail_label="说明",
            detail=detail[:600] if detail else "",
        )
        return _send_feishu_webhook(url, card, secret=str(config.get("webhook_secret") or ""))
    if provider == "wecom":
        return _send_wecom_webhook(url, text)
    return _send_generic_webhook(
        url,
        {
            "event": "task_progress_event",
            "task_id": task_id,
            "task_title": task_title,
            "task_event": event,
            "phase": phase,
            "level": level,
            "status": status,
            "project": project_name,
            "message": message,
            "summary": summary,
        },
    )


def _send_desktop_notification(
    *,
    title: str,
    body: str,
    icon: str = "dialog-information",
) -> bool:
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
    icon = (icon or "dialog-information")[:80]
    system = platform.system().lower()

    def _run_windows_msg() -> bool:
        msg = shutil.which("msg.exe") or shutil.which("msg")
        if not msg:
            return False
        result = subprocess.run(
            [msg, "*", "/time:10", f"{title}\n{body}".strip()],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0

    def _escape_applescript(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def _escape_xml(value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _windows_toast_xml() -> str:
        lines = [title, *[line for line in body.splitlines() if line.strip()]]
        text_nodes = "\n".join(
            f"        <text>{_escape_xml(line)}</text>" for line in lines[:4]
        )
        return (
            "<toast>\n"
            "  <visual>\n"
            "    <binding template=\"ToastGeneric\">\n"
            f"{text_nodes}\n"
            "      <text placement=\"attribution\">CodePilot</text>\n"
            "    </binding>\n"
            "  </visual>\n"
            "</toast>"
        )

    try:
        if system == "darwin":
            script = (
                f'display notification "{_escape_applescript(body)}" '
                f'with title "{_escape_applescript(title)}"'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
            return result.returncode == 0
        if system == "linux":
            if shutil.which("notify-send"):
                result = subprocess.run(
                    ["notify-send", "-i", icon, title, body],
                    capture_output=True, timeout=3,
                )
                return result.returncode == 0
            return False
        if system == "windows":
            # PowerShell one-liner using Windows Runtime ToastNotification.
            toast_xml = _windows_toast_xml()
            ps_cmd = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
                f"$xml = @'\n{toast_xml}\n'@\n"
                "$doc = [Windows.Data.Xml.Dom.XmlDocument]::new();"
                "$doc.LoadXml($xml);"
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('CodePilot').Show($toast);"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return True
            return _run_windows_msg()
    except Exception:
        if system == "windows":
            return _run_windows_msg()
        return False
    return False

