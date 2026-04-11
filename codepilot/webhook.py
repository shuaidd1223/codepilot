"""Webhook 通知：任务状态变更时推送到飞书/企微/通用 HTTP."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional


def _get_webhook_config(project_path: str) -> dict:
    """从 AGENTS.toml 读取 webhook 配置."""
    toml_path = Path(project_path) / "AGENTS.toml"
    config = {"webhook_url": "", "enabled": False}

    if not toml_path.exists():
        return config

    try:
        content = toml_path.read_text(encoding="utf-8")
        in_notifications = False
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("[notifications]"):
                in_notifications = True
                continue
            if line.startswith("[") and not line.startswith("[notifications]"):
                in_notifications = False
            if in_notifications and "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key in config:
                    if key == "enabled":
                        config[key] = val.lower() in ("true", "1", "yes")
                    else:
                        config[key] = val
    except Exception:
        pass

    # 环境变量覆盖
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

    return ok
