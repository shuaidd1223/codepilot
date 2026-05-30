"""Feishu bot helper functions."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from codepilot.core.runtime import is_process_alive
from codepilot.feishu_cards import (
    _TASK_STATUS_FILTER_ALIASES,
    _TASK_STATUS_FILTER_ORDER,
    _count_fields,
    _field,
    _field_block,
    _hr,
    _running_label,
    _status_label,
)
from codepilot.feishu_config import FeishuBotConfig
from codepilot.storage import database as db


@dataclass
class _CommandContext:
    cfg: FeishuBotConfig
    config_path: Path | None = None
    chat_id: str = ""


def _chat_scope(chat_id: str) -> str:
    from codepilot.feishu_bot.constants import _CHAT_CONTEXT_PREFIX

    raw = str(chat_id or "").strip()
    return f"{_CHAT_CONTEXT_PREFIX}{raw}" if raw else ""


def _chat_meta(chat_id: str) -> tuple[str, dict[str, Any]]:
    from codepilot.feishu_bot.constants import _CHAT_CONTEXT_SERVICE

    scope = _chat_scope(chat_id)
    if not scope:
        return "", {}
    state = db.get_service_state(_CHAT_CONTEXT_SERVICE, scope)
    if not state:
        state = db.get_service_state(_CHAT_CONTEXT_SERVICE, str(chat_id or "").strip())
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    return scope, dict(meta)


def _write_chat_meta(chat_id: str, meta: dict[str, Any]) -> None:
    from codepilot.feishu_bot.constants import _CHAT_CONTEXT_SERVICE

    scope = _chat_scope(chat_id)
    if not scope:
        return
    next_meta = dict(meta or {})
    next_meta.update({"chat_id": str(chat_id or "").strip(), "last_seen_at": _now_iso()})
    db.upsert_service_state(
        _CHAT_CONTEXT_SERVICE,
        scope,
        pid=0,
        status="context",
        log_path="",
        meta=next_meta,
    )


def _task_filter_label(status_filter: str) -> str:
    normalized = str(status_filter or "all").strip().lower() or "all"
    if normalized == "all":
        return "全部"
    return _status_label(normalized)


def _normalize_task_status_filter(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return "all"
    normalized = _TASK_STATUS_FILTER_ALIASES.get(value)
    if normalized:
        return normalized
    allowed = " / ".join(_task_filter_label(item) for item in _TASK_STATUS_FILTER_ORDER)
    raise RuntimeError(f"不支持的状态筛选 `{raw}`。可用值：{allowed}。")


def _parse_positive_page(raw: str) -> int:
    try:
        page = int(str(raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("页码必须是正整数。") from exc
    if page <= 0:
        raise RuntimeError("页码必须大于 0。")
    return page


def _build_tasks_command(project_name: str, *, status_filter: str = "all", page: int = 1) -> str:
    command = f"tasks {project_name}".strip()
    normalized_status = _normalize_task_status_filter(status_filter)
    command += f" status={normalized_status}"
    if int(page or 1) > 1:
        command += f" page={int(page)}"
    return command


def _parse_tasks_command_args(tokens: list[str], *, default_project: str = "") -> tuple[str, str, int]:
    project_tokens: list[str] = []
    status_filter = "all"
    page = 1

    for token in tokens:
        text = str(token or "").strip()
        if not text:
            continue
        lower = text.lower()
        if "=" in text:
            key, value = text.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in {"status", "state", "filter", "状态", "筛选"}:
                status_filter = _normalize_task_status_filter(value)
                continue
            if key in {"page", "p", "页", "页码"}:
                page = _parse_positive_page(value)
                continue
        if lower in _TASK_STATUS_FILTER_ALIASES:
            status_filter = _normalize_task_status_filter(text)
            continue
        if re.fullmatch(r"\d+", text):
            page = _parse_positive_page(text)
            continue
        project_tokens.append(text)

    if len(project_tokens) > 1:
        raise RuntimeError(
            "tasks 命令参数过多。请使用 `tasks <project> status=<状态> page=<页码>`。"
        )
    project_name = _resolve_project(project_tokens[0] if project_tokens else "", default_project=default_project)
    return project_name, status_filter, page


def _card_commands(kind: str, *, project: str = "", task_id: int | None = None) -> list[tuple[str, str]]:
    if kind == "global":
        return [
            ("projects", "项目清单"),
            ("use <project>", "进入项目"),
            ("services all", "全局服务"),
            ("global", "刷新总览"),
        ]
    if kind == "projects":
        return [
            ("use <project>", "进入项目"),
            ("overview <project>", "项目总览"),
            ("tasks <project>", "任务面板"),
            ("project info <project>", "项目信息"),
            ("project delete <project>", "删除项目"),
            ("global", "全局状态"),
        ]
    if kind == "project_manage":
        target = project or "<project>"
        return [
            (f"project info {target}", "项目信息"),
            (f"use {target}", "进入项目"),
            (f"tasks {target}", "任务面板"),
            (f"services {target}", "服务状态"),
            (f"project delete {target}", "删除项目"),
            ("projects", "项目清单"),
        ]
    if kind == "project":
        target = project or "<project>"
        return [
            ("tasks", "任务列表"),
            ("requirements", "会话记录"),
            ("services", "服务状态"),
            ("global", "全局状态"),
            (f"daemon start {target}", "启动轮询"),
            (f"inspect start {target}", "启动巡检"),
        ]
    if kind == "tasks":
        return [
            ("detail <id>", "任务详情"),
            ("logs <id>", "任务日志"),
            ("stop <id>", "停止执行"),
            ("retry <id>", "重试任务"),
            ("cancel <id>", "取消待办"),
            ("delete <id>", "删除任务"),
        ]
    if kind == "task" and task_id:
        return [
            (f"detail {task_id}", "任务详情"),
            (f"logs {task_id}", "查看日志"),
            (f"stop {task_id}", "停止执行"),
            (f"retry {task_id}", "重试任务"),
            (f"cancel {task_id}", "取消任务"),
            (f"archive {task_id}", "归档任务"),
            (f"delete {task_id}", "删除任务"),
            ("tasks", "任务面板"),
        ]
    if kind == "sessions":
        return [
            ("ask <text>", "发送到 OpenCode"),
            ("session <id>", "会话详情"),
            ("requirements", "刷新会话"),
            ("tasks", "任务列表"),
            ("overview", "项目总览"),
        ]
    if kind == "session":
        return [
            ("requirements", "返回列表"),
            ("tasks", "相关任务"),
            ("overview", "项目总览"),
        ]
    if kind == "services":
        target = project or "<project>"
        return [
            (f"daemon start {target}", "启动轮询"),
            (f"daemon stop {target}", "停止轮询"),
            (f"inspect start {target}", "启动巡检"),
            (f"inspect stop {target}", "停止巡检"),
            ("services", "刷新状态"),
            ("global", "全局状态"),
        ]
    return []


def _task_list_commands(
    tasks: list[dict[str, Any]],
    *,
    project_name: str,
    status_filter: str,
    page: int,
    total_pages: int,
) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = []
    if tasks:
        task_id = int(tasks[0]["id"])
        commands.extend(
            [
                (f"detail {task_id}", "查看首个任务"),
                (f"logs {task_id}", "查看日志"),
                (f"stop {task_id}", "停止执行"),
                (f"retry {task_id}", "重试任务"),
                (f"cancel {task_id}", "取消任务"),
                (f"archive {task_id}", "归档任务"),
                (f"delete {task_id}", "删除任务"),
            ]
        )
    if page > 1:
        commands.append((_build_tasks_command(project_name, status_filter=status_filter, page=page - 1), "上一页"))
    if page < total_pages:
        commands.append((_build_tasks_command(project_name, status_filter=status_filter, page=page + 1), "下一页"))

    for quick_filter in ("all", "in_progress", "backlog", "failed", "done"):
        if quick_filter == status_filter:
            continue
        commands.append(
            (_build_tasks_command(project_name, status_filter=quick_filter, page=1), f"看{_task_filter_label(quick_filter)}")
        )

    if not commands:
        commands = _card_commands("tasks")
    return commands[:10]


def _task_count(project_name: str) -> int:
    return len([task for task in db.list_tasks(project=project_name) if str(task.get("status") or "") != "archived"])


def _session_count(project_name: str) -> int:
    return len(db.list_sessions(project=project_name))


def _runtime_service_state(service: str, scope: str = "_global") -> dict[str, Any]:
    state = db.get_service_state(service, scope)
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    try:
        pid = int(state.get("pid") or 0) if state else 0
    except Exception:
        pid = 0
    running = bool(pid and is_process_alive(pid))
    return {
        "running": running,
        "pid": pid if running else 0,
        "started_at": meta.get("started_at") or "",
        "log": str(state.get("log_path") or "") if state else "",
    }


def _project_service_status(project_name: str, service: str) -> dict[str, Any]:
    if service == "tasks":
        from codepilot.commands.daemon import daemon_service_status

        return daemon_service_status(project_name)
    if service == "inspect":
        from codepilot.commands.inspect import inspect_service_status

        return inspect_service_status(project_name)
    raise RuntimeError(f"未知服务：{service}")


def _load_chat_project(chat_id: str) -> str:
    _scope, meta = _chat_meta(chat_id)
    project = str(meta.get("project") or "").strip()
    found = db.get_project(project) if project else None
    if found:
        return str(found["name"])
    return ""


def _save_chat_project(chat_id: str, project_name: str) -> None:
    from codepilot.feishu_bot.constants import _PENDING_ACTION_OPTIONS_KEY, _PENDING_GOAL_TEXT_KEY

    _scope, meta = _chat_meta(chat_id)
    meta["project"] = project_name
    meta.pop(_PENDING_ACTION_OPTIONS_KEY, None)
    meta.pop(_PENDING_GOAL_TEXT_KEY, None)
    _write_chat_meta(chat_id, meta)
    _clear_pending_confirm(chat_id)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _touch_chat_seen(chat_id: str) -> None:
    _scope, meta = _chat_meta(chat_id)
    if not _scope:
        return
    _write_chat_meta(chat_id, meta)


def _notification_chat_ids(project_name: str = "") -> list[str]:
    from codepilot.feishu_bot.constants import _CHAT_CONTEXT_PREFIX, _CHAT_CONTEXT_SERVICE

    rows = db.list_service_states(_CHAT_CONTEXT_SERVICE)
    chat_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        scope = str(row.get("scope") or "")
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        if scope.startswith(_CHAT_CONTEXT_PREFIX):
            chat_id = str(meta.get("chat_id") or scope[len(_CHAT_CONTEXT_PREFIX):]).strip()
        else:
            chat_id = str(meta.get("chat_id") or scope).strip()
        if not chat_id or chat_id in seen:
            continue
        selected_project = str(meta.get("project") or "").strip()
        if project_name and selected_project and selected_project != project_name:
            continue
        seen.add(chat_id)
        chat_ids.append(chat_id)
    return chat_ids


def _active_project(cfg: FeishuBotConfig, chat_id: str = "") -> str:
    selected = _load_chat_project(chat_id)
    selected_project = db.get_project(selected) if selected else None
    if selected_project:
        return str(selected_project["name"])
    default_project = str(cfg.default_project or "").strip()
    default_project_info = db.get_project(default_project) if default_project else None
    if default_project_info:
        return str(default_project_info["name"])
    return ""


def _load_pending_action_options(chat_id: str) -> list[dict[str, Any]]:
    from codepilot.feishu_bot.constants import _PENDING_ACTION_OPTIONS_KEY

    _scope, meta = _chat_meta(chat_id)
    options = meta.get(_PENDING_ACTION_OPTIONS_KEY)
    if not isinstance(options, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in options:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        label = str(item.get("label") or command).strip()
        if not command:
            continue
        normalized.append({"command": command, "label": label})
    return normalized


def _save_pending_action_options(chat_id: str, options: list[dict[str, Any]]) -> None:
    from codepilot.feishu_bot.constants import _PENDING_ACTION_OPTIONS_KEY

    if not chat_id:
        return
    _scope, meta = _chat_meta(chat_id)
    meta[_PENDING_ACTION_OPTIONS_KEY] = [
        {
            "command": str(item.get("command") or "").strip(),
            "label": str(item.get("label") or item.get("command") or "").strip(),
        }
        for item in (options or [])
        if str(item.get("command") or "").strip()
    ]
    _write_chat_meta(chat_id, meta)


def _clear_pending_action_options(chat_id: str) -> None:
    from codepilot.feishu_bot.constants import _PENDING_ACTION_OPTIONS_KEY

    if not chat_id:
        return
    _scope, meta = _chat_meta(chat_id)
    if _PENDING_ACTION_OPTIONS_KEY in meta:
        meta.pop(_PENDING_ACTION_OPTIONS_KEY, None)
        _write_chat_meta(chat_id, meta)


def _load_pending_goal_text(chat_id: str) -> str:
    from codepilot.feishu_bot.constants import _PENDING_GOAL_TEXT_KEY

    _scope, meta = _chat_meta(chat_id)
    return str(meta.get(_PENDING_GOAL_TEXT_KEY) or "").strip()


def _save_pending_goal_text(chat_id: str, text: str) -> None:
    from codepilot.feishu_bot.constants import _PENDING_GOAL_TEXT_KEY

    if not chat_id:
        return
    _scope, meta = _chat_meta(chat_id)
    value = str(text or "").strip()
    if value:
        meta[_PENDING_GOAL_TEXT_KEY] = value
    else:
        meta.pop(_PENDING_GOAL_TEXT_KEY, None)
    _write_chat_meta(chat_id, meta)


def _clear_pending_goal_text(chat_id: str) -> None:
    from codepilot.feishu_bot.constants import _PENDING_GOAL_TEXT_KEY

    if not chat_id:
        return
    _scope, meta = _chat_meta(chat_id)
    if _PENDING_GOAL_TEXT_KEY in meta:
        meta.pop(_PENDING_GOAL_TEXT_KEY, None)
        _write_chat_meta(chat_id, meta)


def _load_active_opencode_session_id(chat_id: str, project_name: str) -> int | None:
    from codepilot.feishu_bot.constants import _ACTIVE_OPENCODE_SESSION_KEY

    if not chat_id:
        return None
    _scope, meta = _chat_meta(chat_id)
    try:
        session_id = int(meta.get(_ACTIVE_OPENCODE_SESSION_KEY) or 0)
    except (TypeError, ValueError):
        return None
    if session_id <= 0:
        return None
    session = db.get_session(session_id)
    if not session:
        return None
    session_project = str(session.get("project") or "").strip()
    if session_project != project_name:
        return None
    return session_id


def _save_active_opencode_session_id(chat_id: str, session_id: int) -> None:
    from codepilot.feishu_bot.constants import _ACTIVE_OPENCODE_SESSION_KEY

    if not chat_id or session_id <= 0:
        return
    _scope, meta = _chat_meta(chat_id)
    meta[_ACTIVE_OPENCODE_SESSION_KEY] = int(session_id)
    _write_chat_meta(chat_id, meta)


def _feishu_session_title(text: str) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:40] or "OpenCode 会话"


def _resolve_opencode_db_session(
    project_name: str,
    user_text: str,
    *,
    chat_id: str,
    session_id: int | None = None,
) -> tuple[int, str]:
    if session_id is not None:
        session = db.get_session(int(session_id))
        if not session:
            raise RuntimeError(f"会话 #{session_id} 不存在。")
        session_project = str(session.get("project") or "").strip()
        project = db.get_project(session_project)
        if not project:
            raise RuntimeError(f"会话 #{session_id} 所属项目 '{session_project}' 未注册。")
        canonical_project = str(project["name"])
        _save_active_opencode_session_id(chat_id, int(session_id))
        return int(session_id), canonical_project

    project = db.get_project(project_name)
    if not project:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    canonical_project = str(project["name"])
    active_id = _load_active_opencode_session_id(chat_id, canonical_project)
    if active_id:
        return active_id, canonical_project
    session = db.create_session(canonical_project, _feishu_session_title(user_text))
    resolved_id = int(session["id"])
    _save_active_opencode_session_id(chat_id, resolved_id)
    return resolved_id, canonical_project


def _confirm_scope(chat_id: str) -> str:
    from codepilot.feishu_bot.constants import _PENDING_CONFIRM_DIRECT_SCOPE

    raw = str(chat_id or "").strip()
    return raw or _PENDING_CONFIRM_DIRECT_SCOPE


def _parse_iso_datetime(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _generate_confirm_token(chat_id: str, command_text: str) -> str:
    seed = "|".join(
        [
            _confirm_scope(chat_id),
            str(command_text or "").strip(),
            _now_iso(),
            str(os.getpid()),
        ]
    )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6].upper()


def _load_pending_confirm(chat_id: str, *, allow_expired: bool = False) -> dict[str, Any] | None:
    from codepilot.feishu_bot.constants import _PENDING_CONFIRM_SERVICE

    state = db.get_service_state(_PENDING_CONFIRM_SERVICE, _confirm_scope(chat_id))
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    if not meta:
        return None
    token = str(meta.get("token") or "").strip().upper()
    action = str(meta.get("action") or "").strip()
    expires_at = _parse_iso_datetime(meta.get("expires_at"))
    if not token or not action or expires_at is None:
        db.clear_service_state(_PENDING_CONFIRM_SERVICE, _confirm_scope(chat_id))
        return None
    if not allow_expired and expires_at <= datetime.now():
        db.clear_service_state(_PENDING_CONFIRM_SERVICE, _confirm_scope(chat_id))
        return None
    return dict(meta)


def _save_pending_confirm(chat_id: str, pending: dict[str, Any]) -> dict[str, Any]:
    from codepilot.feishu_bot.constants import _PENDING_CONFIRM_SERVICE

    token = str(pending.get("token") or "").strip().upper()
    action = str(pending.get("action") or "").strip()
    if not token or not action:
        raise RuntimeError("确认上下文不完整。")
    meta = dict(pending)
    meta["token"] = token
    meta["action"] = action
    meta["scope"] = _confirm_scope(chat_id)
    return db.upsert_service_state(
        _PENDING_CONFIRM_SERVICE,
        _confirm_scope(chat_id),
        pid=0,
        status="pending",
        log_path="",
        meta=meta,
    )


def _clear_pending_confirm(chat_id: str) -> bool:
    from codepilot.feishu_bot.constants import _PENDING_CONFIRM_SERVICE

    return db.clear_service_state(_PENDING_CONFIRM_SERVICE, _confirm_scope(chat_id))


def _task_confirm_lines(task_ids: list[int]) -> list[str]:
    lines: list[str] = []
    for task_id in task_ids:
        task = db.get_task(task_id)
        if not task:
            raise RuntimeError(f"任务 #{task_id} 不存在。")
        title = str(task.get("title") or "").strip() or f"任务 #{task_id}"
        status = _status_label(str(task.get("status") or ""))
        lines.append(f"- `#{task_id}` {title} / {status}")
    return lines


def _pending_delete_confirm(task_ids: list[int], *, command_text: str, chat_id: str) -> dict[str, Any]:
    from codepilot.feishu_bot.constants import _PENDING_CONFIRM_TTL_SECONDS

    expires_at = datetime.now() + timedelta(seconds=_PENDING_CONFIRM_TTL_SECONDS)
    return {
        "token": _generate_confirm_token(chat_id, command_text),
        "action": "delete_task_batch" if len(task_ids) > 1 else "delete_task",
        "command": command_text,
        "summary": f"批量删除 {len(task_ids)} 个任务" if len(task_ids) > 1 else f"删除任务 #{task_ids[0]}",
        "task_ids": list(task_ids),
        "details": _task_confirm_lines(task_ids),
        "created_at": _now_iso(),
        "expires_at": expires_at.isoformat(timespec="seconds"),
    }


def _pending_project_delete_confirm(project_name: str, *, command_text: str, chat_id: str) -> dict[str, Any]:
    from codepilot.feishu_bot.constants import _PENDING_CONFIRM_TTL_SECONDS

    project = db.get_project(project_name)
    if not project:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    stats = db.get_task_stats(project_name)
    expires_at = datetime.now() + timedelta(seconds=_PENDING_CONFIRM_TTL_SECONDS)
    return {
        "token": _generate_confirm_token(chat_id, command_text),
        "action": "delete_project",
        "command": command_text,
        "summary": f"删除项目 {project_name}",
        "project_name": project_name,
        "details": [
            f"- 项目：`{project_name}`",
            f"- 路径：`{project['path']}`",
            f"- 关联任务：`{int(stats.get('total') or 0)}`",
            "- 工作目录不会被删除",
        ],
        "created_at": _now_iso(),
        "expires_at": expires_at.isoformat(timespec="seconds"),
    }


def _format_service_state(name: str, status: dict[str, Any]) -> str:
    state = _running_label(status)
    text = f"**{name}** `{state}` / PID `{status.get('pid') or 0}`"
    started_at = str(status.get("started_at") or "").strip()
    if started_at:
        text += f"\n启动时间：`{started_at}`"
    return text


def _resolve_project(explicit: str, *, default_project: str = "") -> str:
    name = str(explicit or "").strip()
    if name:
        project = db.get_project(name)
        if not project:
            raise RuntimeError(f"项目 '{name}' 未注册。")
        return str(project["name"])
    default_name = str(default_project or "").strip()
    if default_name:
        project = db.get_project(default_name)
        if project:
            return str(project["name"])
    projects = db.list_projects()
    if len(projects) == 1:
        return str(projects[0]["name"])
    raise RuntimeError("请显式指定项目，例如 `tasks demo` 或先执行 `projects`。")


def _project_status_blocks(project_name: str) -> list[str | dict[str, Any]]:
    project = db.get_project(project_name)
    if project is None:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    stats = db.get_task_stats(project_name)
    daemon_status = _project_service_status(project_name, "tasks")
    inspect_status = _project_service_status(project_name, "inspect")
    return [
        _field_block(
            [
                _field(f"**项目**\n`{project_name}`"),
                _field(f"**路径**\n`{project.get('path') or ''}`", is_short=False),
                _field(f"**配置**\n`{project.get('config_file') or '-'}`", is_short=False),
                _field(f"**任务总数**\n`{_task_count(project_name)}`"),
                _field(f"**会话总数**\n`{_session_count(project_name)}`"),
                *_count_fields(stats),
            ]
        ),
        _hr(),
        _field_block(
            [
                _field(f"**任务轮询**\n`{_running_label(daemon_status)}`\nPID `{daemon_status.get('pid') or 0}`"),
                _field(f"**巡检**\n`{_running_label(inspect_status)}`\nPID `{inspect_status.get('pid') or 0}`"),
                _field(f"**轮询启动时间**\n`{daemon_status.get('started_at') or '-'}`"),
                _field(f"**巡检启动时间**\n`{inspect_status.get('started_at') or '-'}`"),
            ]
        ),
    ]


def _message_task_ids(message: dict[str, Any]) -> list[int]:
    import json

    raw = message.get("task_ids")
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = []
        values = parsed if isinstance(parsed, list) else []
    else:
        values = []
    task_ids: list[int] = []
    seen: set[int] = set()
    for item in values:
        try:
            task_id = int(item)
        except (TypeError, ValueError):
            continue
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        task_ids.append(task_id)
    return task_ids


def _session_related_task_ids(messages: list[dict[str, Any]], *, limit: int = 4) -> list[int]:
    related: list[int] = []
    seen: set[int] = set()
    for message in reversed(messages):
        for task_id in _message_task_ids(message):
            if task_id in seen:
                continue
            seen.add(task_id)
            related.append(task_id)
            if len(related) >= max(1, int(limit or 1)):
                return related
    return related


def _session_followup_commands(session_id: int, *, related_task_ids: list[int] | None = None) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = [
        (f"session {session_id}", "刷新会话"),
    ]
    task_ids = related_task_ids or []
    if task_ids:
        commands.extend(
            [
                (f"detail {task_ids[0]}", "查看任务"),
                (f"logs {task_ids[0]}", "查看日志"),
            ]
        )
    else:
        commands.append(("tasks", "任务列表"))
    commands.extend(
        [
            ("requirements", "返回会话列表"),
            ("overview", "项目总览"),
        ]
    )
    return commands


def _event_template(level: str, event: str) -> str:
    if level == "error" or event in {"failed", "cancelled", "merge_failed"}:
        return "red"
    if event in {"done", "merged", "review_pass", "phase_end"}:
        return "green"
    if event in {"review_fail", "requeued", "preflight_skip"}:
        return "orange"
    if event in {"phase_start", "started"}:
        return "blue"
    return "purple"


def _event_title(event: str, phase: str = "") -> str:
    from codepilot.feishu_cards import _phase_label

    label = _phase_label(phase)
    return {
        "started": "任务开始执行",
        "phase_start": f"{label} 开始",
        "phase_end": f"{label} 完成",
        "review_pass": "Review 通过",
        "review_fail": "Review 未通过",
        "merged": "任务分支已合并",
        "merge_failed": "任务合并失败",
        "done": "任务已完成",
        "failed": "任务失败",
        "cancelled": "任务已停止",
        "requeued": "任务已回队列",
        "preflight_skip": "任务预检跳过",
    }.get(event, "任务状态变更")
