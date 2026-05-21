"""Task event notification card builders for Feishu bot."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from codepilot.feishu_bot.constants import _NOTIFY_DEDUPE_SERVICE
from codepilot.feishu_bot.helpers import (
    _card_commands,
    _event_template,
    _event_title,
    _notification_chat_ids,
    _now_iso,
)
from codepilot.feishu_cards import (
    _card,
    _column_panels,
    _command_panel,
    _phase_label,
    _plain_block,
    _section,
    _status_mark,
)
from codepilot.feishu_config import load_feishu_bot_config
from codepilot.storage import database as db

_BotCardSender = Callable[..., bool]


def _feishu_notify_script() -> Path:
    from codepilot.feishu_runtime import notify_script

    return notify_script()


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _external_notifications_allowed() -> bool:
    if _truthy_env("CODEPILOT_SUPPRESS_EXTERNAL_NOTIFICATIONS"):
        return False
    if "PYTEST_CURRENT_TEST" in os.environ and not _truthy_env("CODEPILOT_ALLOW_TEST_NOTIFICATIONS"):
        return False
    return True


def _notify_dedupe_scope(
    *,
    project_name: str,
    project_path: str,
    task_id: int,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    status: str = "",
    summary: str = "",
) -> str:
    detail = " ".join((message or summary or "").split())[:1000]
    payload = {
        "project": project_name or Path(project_path).name,
        "task_id": int(task_id),
        "event": str(event or ""),
        "phase": str(phase or ""),
        "level": str(level or ""),
        "status": str(status or ""),
        "detail": detail,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "event:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _was_feishu_notification_sent(scope: str) -> bool:
    state = db.get_service_state(_NOTIFY_DEDUPE_SERVICE, scope)
    return bool(state and state.get("status") == "sent")


def _mark_feishu_notification_sent(scope: str, *, event: str, task_id: int, project_name: str) -> None:
    try:
        db.upsert_service_state(
            _NOTIFY_DEDUPE_SERVICE,
            scope,
            status="sent",
            meta={
                "event": event,
                "task_id": int(task_id),
                "project": project_name,
                "sent_at": _now_iso(),
            },
        )
    except Exception:
        pass


def _send_bot_card(card: dict[str, Any], *, project_name: str = "", chat_ids: list[str] | None = None) -> bool:
    if not _external_notifications_allowed():
        return False
    cfg = load_feishu_bot_config()
    if not cfg.enabled or not cfg.app_id or not cfg.app_secret:
        return False
    targets = chat_ids or _notification_chat_ids(project_name)
    if not targets:
        return False
    payload = json.dumps({"chat_ids": targets, "card": card}, ensure_ascii=False)
    env = os.environ.copy()
    env.update(
        {
            "CODEPILOT_FEISHU_APP_ID": cfg.app_id,
            "CODEPILOT_FEISHU_APP_SECRET": cfg.app_secret,
        }
    )
    try:
        result = subprocess.run(
            [cfg.node_command or "node", str(_feishu_notify_script())],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
            env=env,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    try:
        parsed = json.loads(result.stdout or "{}")
    except Exception:
        return False
    return int(parsed.get("sent") or 0) > 0


def build_task_event_card(
    *,
    project_name: str,
    task_id: int,
    task_title: str,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    status: str = "",
    summary: str = "",
) -> dict[str, Any]:
    blocks: list[str | dict[str, Any]] = [
        _section("事件摘要"),
        *_column_panels(
            [
                f"**项目**\n`{project_name or '-'}`",
                f"**任务**\n`#{task_id}`",
                f"**事件**\n`{_event_title(event, phase)}`",
                f"**状态**\n{_status_mark(status) if status else '-'}",
                f"**阶段**\n`{_phase_label(phase)}`",
            ]
        ),
        _section("标题"),
        _plain_block(task_title),
    ]
    detail = (message or summary or "").strip()
    if detail:
        blocks.extend([_section("说明"), _plain_block(detail[:500])])
    blocks.extend(_command_panel("", _card_commands("task", task_id=task_id)))
    return _card(
        f"CodePilot · {_event_title(event, phase)}",
        blocks,
        template=_event_template(level, event),
        subtitle="任务执行阶段通知。",
    )


def notify_feishu_task_event(
    *,
    project_name: str,
    project_path: str,
    task_id: int,
    task_title: str,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    status: str = "",
    summary: str = "",
    chat_ids: list[str] | None = None,
    _send_card: _BotCardSender | None = None,
) -> bool:
    """Send a proactive task notification to Feishu app chats."""
    db.init_db()
    resolved_project = project_name or Path(project_path).name
    dedupe_scope = _notify_dedupe_scope(
        project_name=resolved_project,
        project_path=project_path,
        task_id=int(task_id),
        event=event,
        phase=phase,
        level=level,
        message=message,
        status=status,
        summary=summary,
    )
    if _was_feishu_notification_sent(dedupe_scope):
        return False
    card = build_task_event_card(
        project_name=resolved_project,
        task_id=int(task_id),
        task_title=task_title,
        event=event,
        phase=phase,
        level=level,
        message=message,
        status=status,
        summary=summary,
    )
    sender = _send_card or _send_bot_card
    sent = sender(card, project_name=resolved_project, chat_ids=chat_ids)
    if sent:
        _mark_feishu_notification_sent(
            dedupe_scope,
            event=event,
            task_id=int(task_id),
            project_name=resolved_project,
        )
    return sent
