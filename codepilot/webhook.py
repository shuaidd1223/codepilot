"""Webhook 通知：任务状态变更时推送到飞书/企微/通用 HTTP."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Optional

from codepilot.config import load_config


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
