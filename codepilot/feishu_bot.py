"""Feishu long-connection bot command routing and card rendering."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from codepilot.core.config import load_config
from codepilot.core.runtime import is_process_alive
from codepilot.nl_command_router import (
    format_numbered_options,
    infer_goal_from_text,
    pick_command_option,
    resolve_natural_language_command,
)
from codepilot.storage import database as db
from codepilot.webapp.display_sort import sort_tasks_for_display
from codepilot.webapp.action_task_ops import (
    archive_task_action,
    batch_task_action,
    cancel_task_action,
    delete_task_action,
    project_service_action,
    stop_task_action,
)
from codepilot.webapp.action_requirements import retry_task_action
from codepilot.webapp.payloads import _compose_log_text, _task_payload, task_detail_payload


@dataclass
class FeishuBotConfig:
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    node_command: str = "node"
    default_project: str = ""
    command_prefix: str = ""


_CHAT_CONTEXT_SERVICE = "feishu_chat"
_CHAT_CONTEXT_PREFIX = "chat:"
_NOTIFY_DEDUPE_SERVICE = "feishu_notify"
_PENDING_REQUIREMENT_KEY = "pending_requirement"
_PENDING_ACTION_OPTIONS_KEY = "pending_action_options"
_PENDING_GOAL_TEXT_KEY = "pending_goal_text"


def _chat_scope(chat_id: str) -> str:
    raw = str(chat_id or "").strip()
    return f"{_CHAT_CONTEXT_PREFIX}{raw}" if raw else ""


def _chat_meta(chat_id: str) -> tuple[str, dict[str, Any]]:
    scope = _chat_scope(chat_id)
    if not scope:
        return "", {}
    state = db.get_service_state(_CHAT_CONTEXT_SERVICE, scope)
    if not state:
        state = db.get_service_state(_CHAT_CONTEXT_SERVICE, str(chat_id or "").strip())
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    return scope, dict(meta)


def _write_chat_meta(chat_id: str, meta: dict[str, Any]) -> None:
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


def load_feishu_bot_config(config_path: Path | None = None) -> FeishuBotConfig:
    cfg = load_config(config_path)
    if cfg is None:
        return FeishuBotConfig()
    section = cfg.feishu_bot if isinstance(cfg.feishu_bot, dict) else {}
    return FeishuBotConfig(
        enabled=bool(cfg.feishu_bot_enabled or section.get("enabled", False)),
        app_id=str(cfg.feishu_app_id or section.get("app_id", "") or ""),
        app_secret=str(cfg.feishu_app_secret or section.get("app_secret", "") or ""),
        node_command=str(cfg.feishu_node_command or section.get("node_command", "node") or "node"),
        default_project=str(cfg.feishu_default_project or section.get("default_project", "") or ""),
        command_prefix=str(cfg.feishu_command_prefix or section.get("command_prefix", "") or "").strip(),
    )


def validate_feishu_bot_config(config_path: Path | None = None) -> list[str]:
    cfg = load_feishu_bot_config(config_path)
    problems: list[str] = []
    if not cfg.enabled:
        problems.append("[feishu_bot].enabled = true 未开启。")
    if not cfg.app_id:
        problems.append("[feishu_bot].app_id 为空。")
    if not cfg.app_secret:
        problems.append("[feishu_bot].app_secret 为空。")
    if not cfg.node_command:
        problems.append("[feishu_bot].node_command 为空。")
    return problems


def _status_label(status: str) -> str:
    return {
        "backlog": "待执行",
        "in_progress": "执行中",
        "done": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "archived": "已归档",
    }.get(str(status or ""), str(status or "-"))


def _md_block(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": str(content or "").strip()}}


def _plain_block(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "plain_text", "content": str(content or "").strip()}}


def _note(content: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": str(content or "").strip()}]}


def _code_block(content: str, *, language: str = "text", title: str = "") -> list[dict[str, Any]]:
    code = str(content or "").rstrip()
    blocks: list[dict[str, Any]] = []
    if title:
        blocks.append(_section(title))
    blocks.append(_md_block(f"```{language}\n{code}\n```"))
    return blocks


def _field(content: str, *, is_short: bool = True) -> dict[str, Any]:
    return {"is_short": is_short, "text": {"tag": "lark_md", "content": str(content or "").strip()}}


def _column(content: str, *, weight: int = 1) -> dict[str, Any]:
    return {
        "tag": "column",
        "width": "weighted",
        "weight": int(weight),
        "vertical_align": "top",
        "elements": [_md_block(content)],
    }


def _column_panel(items: list[str], *, background: str = "grey") -> dict[str, Any]:
    columns = [_column(item) for item in items if str(item or "").strip()]
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": background,
        "columns": columns,
    }


def _column_panels(items: list[str], *, per_row: int = 3, background: str = "grey") -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = []
    size = max(1, int(per_row or 3))
    for index in range(0, len(items), size):
        panels.append(_column_panel(items[index:index + size], background=background))
    return panels


def _field_block(fields: list[str | dict[str, Any]]) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    for item in fields:
        if isinstance(item, dict):
            normalized.append(item)
        elif str(item or "").strip():
            normalized.append(_field(str(item)))
    return {"tag": "div", "fields": normalized}


def _hr() -> dict[str, Any]:
    return {"tag": "hr"}


def _section(label: str, *, hint: str = "") -> dict[str, Any]:
    return _md_block(f"**{label}**")


def _section_note(label: str, hint: str = "") -> list[dict[str, Any]]:
    blocks = [_section(label)]
    if hint:
        blocks.append(_note(hint))
    return blocks


def _status_mark(status: str) -> str:
    value = str(status or "").strip()
    label = _status_label(value)
    if value in {"done", "running", "in_progress"}:
        return f"[OK] {label}"
    if value in {"failed", "cancelled"}:
        return f"[!] {label}"
    if value in {"backlog", "pending"}:
        return f"[..] {label}"
    if value in {"archived"}:
        return f"[-] {label}"
    return label


def _service_mark(status: dict[str, Any]) -> str:
    if status.get("stopping"):
        return "[..] 停止中"
    if status.get("running"):
        return "[OK] 运行中"
    return "[-] 未运行"


def _phase_label(phase: str) -> str:
    value = str(phase or "").strip()
    lower = value.lower()
    if lower.startswith("builder"):
        return value.replace("builder", "Build", 1)
    if lower.startswith("reviewer"):
        return value.replace("reviewer", "Review", 1)
    return {
        "merge": "Merge",
        "commit": "Commit",
        "pending": "Pending",
        "runtime": "Runtime",
        "preflight": "Preflight",
        "done": "Done",
    }.get(lower, value or "-")


def _cmd(prefix: str, command: str) -> str:
    lead = f"{prefix} " if prefix else ""
    return f"`{lead}{command}`"


def _command_panel(prefix: str, commands: list[tuple[str, str]], *, title: str = "下一步命令") -> list[dict[str, Any] | str]:
    if not commands:
        return []
    command_items = [f"**{label}**\n{_cmd(prefix, command)}" for command, label in commands]
    return [_hr(), *_section_note(title, "只展示当前卡片最相关的操作。复制命令发送即可执行。"), *_column_panels(command_items, background="default")]


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
            ("global", "全局状态"),
        ]
    if kind == "project":
        target = project or "<project>"
        return [
            ("tasks", "任务列表"),
            ("requirements", "需求会话"),
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
            (f"logs {task_id}", "查看日志"),
            (f"stop {task_id}", "停止执行"),
            (f"retry {task_id}", "重试任务"),
            (f"cancel {task_id}", "取消任务"),
            (f"archive {task_id}", "归档任务"),
            (f"delete {task_id}", "删除任务"),
        ]
    if kind == "sessions":
        return [
            ("req new <text>", "新建需求会话"),
            ("ask <text>", "新建会话"),
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


def _task_list_commands(tasks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    if not tasks:
        return _card_commands("tasks")
    task_id = int(tasks[0]["id"])
    return [
        (f"detail {task_id}", "查看首个任务"),
        (f"logs {task_id}", "查看日志"),
        (f"stop {task_id}", "停止执行"),
        (f"retry {task_id}", "重试任务"),
        (f"cancel {task_id}", "取消任务"),
        (f"delete {task_id}", "删除任务"),
    ]


def _card(
    title: str,
    blocks: list[str | dict[str, Any]],
    *,
    template: str = "blue",
    note: str = "",
    subtitle: str = "",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if subtitle:
        elements.append(_note(subtitle))
    for block in blocks:
        if isinstance(block, dict):
            elements.append(block)
            continue
        text = str(block or "").strip()
        if not text:
            continue
        elements.append(_md_block(text))
    if note:
        elements.append({"tag": "hr"})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title[:120]},
        },
        "elements": elements,
    }


def _reply_card(card: dict[str, Any]) -> dict[str, Any]:
    return {"type": "interactive", "card": card}


def _help_note(prefix: str) -> str:
    lead = f"{prefix} " if prefix else ""
    return (
        f"命令示例: {lead}global | {lead}use demo | {lead}overview | {lead}tasks | {lead}requirements | "
        f"{lead}需求 优化任务面板 | {lead}答 先做飞书入口 | "
        f"{lead}req new 优化飞书任务卡片 | {lead}ask 帮我梳理一下最近需求 | "
        f"{lead}session reply 12 先做飞书入口 | "
        f"{lead}detail 123 | {lead}logs 123 | {lead}stop 123 | {lead}retry 123 | "
        f"{lead}cancel 12 13 | {lead}archive 20,21 | {lead}delete 30 31 | "
        f"{lead}daemon status | {lead}inspect status"
    )


def _format_count_line(stats: dict[str, Any]) -> str:
    return (
        f"待执行 **{int(stats.get('backlog', 0))}**  "
        f"执行中 **{int(stats.get('in_progress', 0))}**  "
        f"失败 **{int(stats.get('failed', 0))}**  "
        f"完成 **{int(stats.get('done', 0))}**"
    )


def _count_fields(stats: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _field(f"**待执行**\n`{int(stats.get('backlog', 0))}`"),
        _field(f"**执行中**\n`{int(stats.get('in_progress', 0))}`"),
        _field(f"**失败**\n`{int(stats.get('failed', 0))}`"),
        _field(f"**完成**\n`{int(stats.get('done', 0))}`"),
    ]


def _count_panel(stats: dict[str, Any]) -> dict[str, Any]:
    return _column_panel(
        [
            f"**待执行**\n`{int(stats.get('backlog', 0))}`",
            f"**执行中**\n`{int(stats.get('in_progress', 0))}`",
            f"**失败**\n`{int(stats.get('failed', 0))}`",
            f"**完成**\n`{int(stats.get('done', 0))}`",
        ],
        background="grey",
    )


def _running_label(status: dict[str, Any]) -> str:
    if status.get("stopping"):
        return "停止中"
    return "运行中" if status.get("running") else "未运行"


def _priority_badge(priority: str) -> str:
    value = str(priority or "P2").upper()
    return value if value in {"P0", "P1", "P2", "P3"} else "P2"


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
    if project and db.get_project(project):
        return project
    return ""


def _save_chat_project(chat_id: str, project_name: str) -> None:
    _scope, meta = _chat_meta(chat_id)
    meta["project"] = project_name
    meta.pop(_PENDING_REQUIREMENT_KEY, None)
    meta.pop(_PENDING_ACTION_OPTIONS_KEY, None)
    meta.pop(_PENDING_GOAL_TEXT_KEY, None)
    _write_chat_meta(chat_id, meta)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _touch_chat_seen(chat_id: str) -> None:
    _scope, meta = _chat_meta(chat_id)
    if not _scope:
        return
    _write_chat_meta(chat_id, meta)


def _notification_chat_ids(project_name: str = "") -> list[str]:
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
    if selected and db.get_project(selected):
        return selected
    default_project = str(cfg.default_project or "").strip()
    if default_project and db.get_project(default_project):
        return default_project
    return ""


def _load_pending_requirement(chat_id: str, project_name: str) -> dict[str, Any] | None:
    _scope, meta = _chat_meta(chat_id)
    pending = meta.get(_PENDING_REQUIREMENT_KEY)
    if not isinstance(pending, dict):
        return None
    if str(pending.get("project") or "") != project_name:
        return None
    if not str(pending.get("original_title") or "").strip():
        return None
    return dict(pending)


def _save_pending_requirement(chat_id: str, project_name: str, result: dict[str, Any]) -> None:
    if not chat_id:
        return
    _scope, meta = _chat_meta(chat_id)
    meta["project"] = project_name
    meta[_PENDING_REQUIREMENT_KEY] = {
        "project": project_name,
        "original_title": str(result.get("original_title") or "").strip(),
        "qa_history": result.get("qa_history") if isinstance(result.get("qa_history"), list) else [],
        "questions": result.get("questions") if isinstance(result.get("questions"), list) else [],
        "updated_at": _now_iso(),
    }
    _write_chat_meta(chat_id, meta)


def _clear_pending_requirement(chat_id: str) -> None:
    if not chat_id:
        return
    _scope, meta = _chat_meta(chat_id)
    if _PENDING_REQUIREMENT_KEY in meta:
        meta.pop(_PENDING_REQUIREMENT_KEY, None)
        _write_chat_meta(chat_id, meta)


def _load_pending_action_options(chat_id: str) -> list[dict[str, Any]]:
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
    if not chat_id:
        return
    _scope, meta = _chat_meta(chat_id)
    if _PENDING_ACTION_OPTIONS_KEY in meta:
        meta.pop(_PENDING_ACTION_OPTIONS_KEY, None)
        _write_chat_meta(chat_id, meta)


def _load_pending_goal_text(chat_id: str) -> str:
    _scope, meta = _chat_meta(chat_id)
    return str(meta.get(_PENDING_GOAL_TEXT_KEY) or "").strip()


def _save_pending_goal_text(chat_id: str, text: str) -> None:
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
    if not chat_id:
        return
    _scope, meta = _chat_meta(chat_id)
    if _PENDING_GOAL_TEXT_KEY in meta:
        meta.pop(_PENDING_GOAL_TEXT_KEY, None)
        _write_chat_meta(chat_id, meta)


def _format_service_state(name: str, status: dict[str, Any]) -> str:
    state = _running_label(status)
    text = f"**{name}** `{state}` / PID `{status.get('pid') or 0}`"
    started_at = str(status.get("started_at") or "").strip()
    if started_at:
        text += f"\n启动时间：`{started_at}`"
    return text


def build_help_card(*, prefix: str = "", error: str = "") -> dict[str, Any]:
    blocks: list[str | dict[str, Any]] = []
    if error:
        blocks.extend([_section("未识别命令"), _plain_block(error)])
    blocks.extend(
        [
            *_section_note("常用入口", "先进入项目，再查看任务或服务状态。"),
            *_column_panels(
                [
                    f"**全局状态**\n{_cmd(prefix, 'global')}",
                    f"**项目清单**\n{_cmd(prefix, 'projects')}",
                    f"**进入项目**\n{_cmd(prefix, 'use <project>')}",
                    f"**项目总览**\n{_cmd(prefix, 'overview')}",
                    f"**任务面板**\n{_cmd(prefix, 'tasks')}",
                    f"**需求会话**\n{_cmd(prefix, 'requirements')}",
                    f"**提交需求**\n{_cmd(prefix, '需求 <内容>')}",
                    f"**回复澄清**\n{_cmd(prefix, '答 <内容>')}",
                    f"**会话建需求**\n{_cmd(prefix, 'req new <内容>')}",
                    f"**会话继续**\n{_cmd(prefix, 'session reply <id> <内容>')}",
                ],
                background="default",
            ),
            *_section_note("任务操作", "支持单任务和批量任务 ID；多个 ID 可用空格或逗号分隔。"),
            *_column_panels(
                [
                    f"**详情**\n{_cmd(prefix, 'detail <id>')}",
                    f"**日志**\n{_cmd(prefix, 'logs <id>')}",
                    f"**停止**\n{_cmd(prefix, 'stop <id>')}",
                    f"**重试**\n{_cmd(prefix, 'retry <id>')}",
                    f"**批量取消**\n{_cmd(prefix, 'cancel <id...>')}",
                    f"**批量归档**\n{_cmd(prefix, 'archive <id...>')}",
                    f"**批量删除**\n{_cmd(prefix, 'delete <id...>')}",
                ],
                background="default",
            ),
            *_section_note("服务控制", "轮询负责执行任务，巡检负责发现可改进项。"),
            *_column_panels(
                [
                    f"**轮询状态**\n{_cmd(prefix, 'daemon status')}",
                    f"**启动轮询**\n{_cmd(prefix, 'daemon start')}",
                    f"**停止轮询**\n{_cmd(prefix, 'daemon stop')}",
                    f"**巡检状态**\n{_cmd(prefix, 'inspect status')}",
                    f"**启动巡检**\n{_cmd(prefix, 'inspect start')}",
                    f"**停止巡检**\n{_cmd(prefix, 'inspect stop')}",
                ],
                background="default",
            ),
        ]
    )
    return _card("CodePilot 飞书命令", blocks, template="indigo", subtitle="命令按使用场景分组，具体卡片底部会显示更相关的下一步。")


def build_choice_card(message: str, options: list[dict[str, Any]], *, prefix: str = "") -> dict[str, Any]:
    lines = format_numbered_options(message, options)
    blocks: list[str | dict[str, Any]] = [
        _section("请确认操作"),
        _plain_block(lines),
        _note("直接回复数字继续，例如 1。发送新的完整命令会覆盖这次候选。"),
    ]
    blocks.extend(_command_panel(prefix, [("help", "查看命令帮助"), ("projects", "查看项目"), ("tasks", "查看任务")]))
    return _card("CodePilot 操作候选", blocks, template="orange", subtitle="自然语言命中了多个可能操作。")


def build_goal_answer_card(project_name: str, user_text: str, result: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    intent = str(result.get("intent") or "").strip().lower()
    if intent == "clarify" or result.get("job"):
        return build_requirement_result_card(project_name, user_text, result, prefix=prefix)
    if intent == "command":
        return build_help_card(prefix=prefix, error=str(result.get("message") or ""))
    blocks: list[str | dict[str, Any]] = [
        _section("会话结果"),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**类型**\n`{intent or 'question'}`",
            ]
        ),
        _section("你的输入"),
        _plain_block(user_text[:700]),
        _section("系统回复"),
        _plain_block(str(result.get("message") or "").strip()[:1200] or "未获得回复"),
    ]
    blocks.extend(_command_panel(prefix, _card_commands("project", project=project_name)))
    return _card(
        f"CodePilot 会话 · {project_name}",
        blocks,
        template="blue",
        subtitle="自然语言已自动分发到问答或需求流。",
    )


def _submit_goal_from_feishu(project_name: str, user_text: str, *, chat_id: str = "", prefix: str = "") -> dict[str, Any]:
    from codepilot.webapp import actions as web_actions

    if chat_id and project_name:
        _save_chat_project(chat_id, project_name)
    result = web_actions.submit_goal_action(project_name, str(user_text or "").strip())
    if result.get("intent") == "clarify":
        _save_pending_requirement(chat_id, project_name, result)
    else:
        _clear_pending_requirement(chat_id)
    _clear_pending_goal_text(chat_id)
    return _reply_card(build_goal_answer_card(project_name, user_text, result, prefix=prefix))


def _resolve_project(explicit: str, *, default_project: str = "") -> str:
    name = str(explicit or "").strip()
    if name:
        project = db.get_project(name)
        if not project:
            raise RuntimeError(f"项目 '{name}' 未注册。")
        return name
    default_name = str(default_project or "").strip()
    if default_name:
        project = db.get_project(default_name)
        if project:
            return default_name
    projects = db.list_projects()
    if len(projects) == 1:
        return str(projects[0]["name"])
    raise RuntimeError("请显式指定项目，例如 `tasks demo` 或先执行 `projects`。")


def build_projects_card(*, prefix: str = "", default_project: str = "") -> dict[str, Any]:
    db.init_db()
    projects = db.list_projects()
    if not projects:
        return _card(
            "CodePilot 项目",
            [_plain_block("当前还没有已注册项目。先在本机运行 codepilot init <path> 注册项目。")],
            template="orange",
            subtitle="还没有可以管理的项目。",
        )
    total_stats = {"backlog": 0, "in_progress": 0, "failed": 0, "done": 0}
    blocks: list[str | dict[str, Any]] = []
    for project in projects:
        stats = db.get_task_stats(project["name"])
        for key in total_stats:
            total_stats[key] += int(stats.get(key, 0))
    blocks.extend(_section_note("总览", "先看项目规模和整体任务状态。"))
    blocks.extend(
        _column_panels(
            [
                f"**注册项目**\n`{len(projects)}`",
                f"**默认项目**\n`{default_project or '-'}`",
                f"**待执行**\n`{int(total_stats.get('backlog', 0))}`",
                f"**执行中**\n`{int(total_stats.get('in_progress', 0))}`",
                f"**失败**\n`{int(total_stats.get('failed', 0))}`",
                f"**完成**\n`{int(total_stats.get('done', 0))}`",
            ]
        )
    )
    blocks.append(_hr())
    blocks.extend(_section_note("已注册项目", "每个项目一行，便于手机上扫状态。"))
    for project in projects[:12]:
        stats = db.get_task_stats(project["name"])
        project_name = str(project["name"])
        marker = "默认" if project_name == default_project else "已注册"
        blocks.extend(
            _column_panels(
                [
                    f"**项目**\n`{project_name}`\n{marker}",
                    f"**任务 / 会话**\n任务 `{_task_count(project_name)}`\n会话 `{_session_count(project_name)}`",
                    f"**待执行**\n`{int(stats.get('backlog', 0))}`",
                    f"**执行中**\n`{int(stats.get('in_progress', 0))}`",
                    f"**失败**\n`{int(stats.get('failed', 0))}`",
                    f"**完成**\n`{int(stats.get('done', 0))}`",
                ],
                background="default",
            )
        )
        blocks.append(_hr())
    if blocks and blocks[-1].get("tag") == "hr":
        blocks.pop()
    if len(projects) > 12:
        blocks.append(f"还有 `{len(projects) - 12}` 个项目未展示，可用 `projects` 查看完整清单。")
    blocks.extend(_command_panel(prefix, _card_commands("projects")))
    return _card(
        "CodePilot 项目",
        blocks,
        template="blue",
        subtitle="项目清单和任务统计。",
    )


def build_global_status_card(*, prefix: str = "", default_project: str = "") -> dict[str, Any]:
    db.init_db()
    projects = db.list_projects()
    feishu_status = _runtime_service_state("feishu")
    webui_status = _runtime_service_state("webui")
    total_stats = {"backlog": 0, "in_progress": 0, "failed": 0, "done": 0}
    task_services_running = 0
    inspect_services_running = 0
    project_blocks: list[dict[str, Any]] = []

    for project in projects[:12]:
        project_name = str(project["name"])
        stats = db.get_task_stats(project_name)
        for key in total_stats:
            total_stats[key] += int(stats.get(key, 0))
        task_status = _project_service_status(project_name, "tasks")
        inspect_status = _project_service_status(project_name, "inspect")
        if task_status.get("running"):
            task_services_running += 1
        if inspect_status.get("running"):
            inspect_services_running += 1
        label = f"`{project_name}`"
        if project_name == default_project:
            label += " 默认"
        project_blocks.extend(
            _column_panels(
                [
                    f"**项目**\n{label}",
                    f"**服务**\n轮询 `{_service_mark(task_status)}`\n巡检 `{_service_mark(inspect_status)}`",
                    f"**待执行**\n`{int(stats.get('backlog', 0))}`",
                    f"**执行中**\n`{int(stats.get('in_progress', 0))}`",
                    f"**失败**\n`{int(stats.get('failed', 0))}`",
                    f"**完成**\n`{int(stats.get('done', 0))}`",
                    f"**会话**\n`{_session_count(project_name)}`",
                ],
                background="default",
            )
        )

    blocks: list[str | dict[str, Any]] = [
        *_section_note("运行总览", "这个面板用来看整套工具当前是否在线。"),
        *_column_panels(
            [
                f"**注册项目**\n`{len(projects)}`",
                f"**任务轮询**\n`{task_services_running}` / `{len(projects)}`",
                f"**巡检运行**\n`{inspect_services_running}` / `{len(projects)}`",
                f"**飞书长连接**\n`{_service_mark(feishu_status)}`\nPID `{feishu_status.get('pid') or 0}`",
                f"**Web UI**\n`{_service_mark(webui_status)}`\nPID `{webui_status.get('pid') or 0}`",
            ]
        ),
        *_section_note("任务汇总", "所有注册项目的任务状态合计。"),
        _count_panel(total_stats),
    ]
    if feishu_status.get("started_at") or webui_status.get("started_at"):
        blocks.append(
            _field_block(
                [
                    _field(f"**飞书启动时间**\n`{feishu_status.get('started_at') or '-'}`"),
                    _field(f"**Web UI 启动时间**\n`{webui_status.get('started_at') or '-'}`"),
                ]
            )
        )
    if project_blocks:
        blocks.append(_hr())
        blocks.extend(_section_note("项目状态", "轮询和巡检是远程控制的关键状态。"))
        for block in project_blocks:
            blocks.append(block)
            blocks.append(_hr())
        if blocks and blocks[-1].get("tag") == "hr":
            blocks.pop()
    else:
        blocks.append("**项目状态**\n当前还没有已注册项目。")
    if len(projects) > 12:
        blocks.append(f"还有 `{len(projects) - 12}` 个项目未展示，可用 `projects` 查看项目清单。")
    blocks.extend(_command_panel(prefix, _card_commands("global")))
    return _card("CodePilot 全局状态", blocks, template="blue", subtitle="全局运行状态、核心服务和每个项目的执行状态。")


def build_overview_card(project_name: str, *, prefix: str = "") -> dict[str, Any]:
    project = db.get_project(project_name)
    if project is None:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    stats = db.get_task_stats(project_name)
    daemon_status = _project_service_status(project_name, "tasks")
    inspect_status = _project_service_status(project_name, "inspect")
    blocks: list[str | dict[str, Any]] = [
        _field_block(
            [
                _field(f"**项目**\n`{project_name}`"),
                _field(f"**路径**\n`{project.get('path') or ''}`", is_short=False),
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
    blocks.extend(_command_panel(prefix, _card_commands("project", project=project_name)))
    return _card(f"CodePilot 项目总览 · {project_name}", blocks, template="wathet", subtitle="项目路径、任务统计和服务状态。")


def build_tasks_card(project_name: str, *, prefix: str = "") -> dict[str, Any]:
    project = db.get_project(project_name)
    if project is None:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    stats = db.get_task_stats(project_name)
    raw_tasks = [task for task in sort_tasks_for_display(db.list_tasks(project=project_name)) if str(task.get("status") or "") != "archived"]
    tasks = [_task_payload(task) for task in raw_tasks[:8]]
    blocks: list[str | dict[str, Any]] = [
        *_section_note("任务概览", "优先看执行中、失败和待执行任务。"),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**任务总数**\n`{_task_count(project_name)}`",
                f"**待执行**\n`{int(stats.get('backlog', 0))}`",
                f"**执行中**\n`{int(stats.get('in_progress', 0))}`",
                f"**失败**\n`{int(stats.get('failed', 0))}`",
                f"**完成**\n`{int(stats.get('done', 0))}`",
            ]
        )
    ]
    if tasks:
        blocks.append(_hr())
        blocks.extend(_section_note("最近任务", "每条任务都带下一步可复制命令。"))
        for task in tasks:
            runtime = f" / {task['runtime']}" if task.get("runtime") else ""
            latest = str(task.get("latest") or "").strip().replace("\n", " ")
            blocks.extend(
                _column_panels(
                    [
                        f"**任务**\n`#{task['id']}` {task['title'][:56]}",
                        f"**状态**\n`{_status_mark(task['status'])}`",
                        f"**优先级**\n`{_priority_badge(task['priority'])}`",
                        f"**Agent**\n`{task['agent']}`{runtime}",
                    ],
                    background="default",
                )
            )
            if latest:
                blocks.append(f"**最近输出**\n{latest[:120]}")
            blocks.append(_hr())
        if blocks and blocks[-1].get("tag") == "hr":
            blocks.pop()
    else:
        blocks.extend([_section("最近任务"), _plain_block("当前没有可展示的任务。")])
    blocks.extend(_command_panel(prefix, _task_list_commands(tasks)))
    return _card(
        f"CodePilot 任务面板 · {project_name}",
        blocks,
        template="turquoise",
        subtitle="任务列表按当前优先级和状态排序展示。",
    )


def build_sessions_card(project_name: str, *, prefix: str = "") -> dict[str, Any]:
    project = db.get_project(project_name)
    if project is None:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    sessions = db.list_sessions(project=project_name)[:8]
    if not sessions:
        blocks: list[str | dict[str, Any]] = [
            _section("需求会话"),
            _plain_block("当前还没有需求会话。可先在 Web UI 或 CLI 中提交一条需求。"),
        ]
        blocks.extend(_command_panel(prefix, _card_commands("sessions")))
        return _card(
            f"CodePilot 需求会话 · {project_name}",
            blocks,
            template="orange",
            subtitle="需求会话用于查看规划和澄清上下文。",
        )
    blocks: list[str | dict[str, Any]] = [
        *_section_note("需求会话", "最近会话按更新时间展示。"),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**会话数**\n`{_session_count(project_name)}`",
            ]
        ),
        _hr(),
    ]
    for session in sessions:
        messages = db.list_session_messages(int(session["id"]))
        blocks.extend(
            _column_panels(
                [
                    f"**会话**\n`#{session['id']}` {str(session.get('title') or '新会话')[:56]}",
                    f"**状态**\n`{session.get('status') or '-'}`",
                    f"**消息数**\n`{len(messages)}`",
                    f"**更新时间**\n`{session.get('updated_at') or '-'}`",
                ],
                background="default",
            )
        )
        blocks.append(_hr())
    if blocks and blocks[-1].get("tag") == "hr":
        blocks.pop()
    blocks.extend(_command_panel(prefix, _card_commands("sessions")))
    return _card(
        f"CodePilot 需求会话 · {project_name}",
        blocks,
        template="carmine",
        subtitle="查看需求规划、澄清和相关任务。",
    )


def _message_task_ids(message: dict[str, Any]) -> list[int]:
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
        (f"session reply {session_id} <text>", "继续会话"),
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


def build_session_result_card(
    session_id: int,
    user_text: str,
    result: dict[str, Any],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    messages = db.list_session_messages(session_id)
    intent = str(result.get("intent") or "info").strip().lower() or "info"
    task_ids = []
    raw_task_ids = result.get("task_ids")
    if isinstance(raw_task_ids, list):
        for item in raw_task_ids:
            try:
                task_id = int(item)
            except (TypeError, ValueError):
                continue
            if task_id > 0:
                task_ids.append(task_id)
    if not task_ids:
        task_ids = _session_related_task_ids(messages)
    state_label = {
        "clarify": "等待你继续",
        "requirement": "已进入规划",
        "task": "已进入规划",
        "question": "已回复",
        "command": "命令提示",
        "info": "会话提示",
    }.get(intent, "会话已更新")
    message = str(result.get("message") or "").strip() or "未获得回复"
    blocks: list[str | dict[str, Any]] = [
        _section("会话结果"),
        *_column_panels(
            [
                f"**项目**\n`{session.get('project') or '-'}`",
                f"**会话**\n`#{session_id}`",
                f"**状态**\n`{state_label}`",
                f"**消息数**\n`{len(messages)}`",
            ]
        ),
        _section("你的输入"),
        _plain_block(str(user_text or "").strip()[:700]),
        _section("系统回复"),
        _plain_block(message[:1200]),
    ]
    refined_title = str(result.get("refined_title") or "").strip()
    if refined_title:
        blocks.extend([_section("当前规划标题"), _plain_block(refined_title[:500])])
    if intent == "clarify":
        questions = result.get("questions") if isinstance(result.get("questions"), list) else []
        if questions:
            blocks.extend(_code_block(_question_lines(questions), title="请继续回复"))
        blocks.append(_note(f"继续请发送：session reply {session_id} <你的补充信息>"))
    if task_ids:
        task_text = "、".join(f"#{task_id}" for task_id in task_ids)
        blocks.extend([_section("相关任务"), _plain_block(task_text)])
    blocks.extend(_command_panel(prefix, _session_followup_commands(session_id, related_task_ids=task_ids)))
    title = {
        "clarify": f"需求会话待继续 · #{session_id}",
        "requirement": f"需求会话已提交 · #{session_id}",
        "task": f"需求会话已提交 · #{session_id}",
        "question": f"需求会话已回复 · #{session_id}",
        "command": f"需求会话提示 · #{session_id}",
        "info": f"需求会话提示 · #{session_id}",
    }.get(intent, f"需求会话已更新 · #{session_id}")
    template = "orange" if intent in {"clarify", "info"} else ("green" if intent in {"requirement", "task"} else "blue")
    return _card(title, blocks, template=template, subtitle="需求会话的最新回复和最相关的继续命令。")


def build_session_card(session_id: int, *, prefix: str = "") -> dict[str, Any]:
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    messages = db.list_session_messages(session_id)
    related_task_ids = _session_related_task_ids(messages)
    pending_reply = bool(messages and str(messages[-1].get("role") or "") == "assistant" and str(messages[-1].get("intent") or "") == "clarify")
    blocks: list[str | dict[str, Any]] = [
        *_section_note("会话状态", "最近消息在下方展示。"),
        *_column_panels(
            [
                f"**项目**\n`{session.get('project') or '-'}`",
                f"**状态**\n`{session.get('status') or '-'}`",
                f"**更新时间**\n`{session.get('updated_at') or '-'}`",
                f"**消息数**\n`{len(messages)}`",
            ]
        ),
        _section("标题"),
        _plain_block(session.get("title") or "新会话"),
    ]
    if pending_reply:
        blocks.append(_note(f"当前会话正在等待补充信息。继续请发送：session reply {session_id} <你的补充信息>"))
    if messages:
        lines: list[str] = []
        for message in messages[-6:]:
            role = str(message.get("role") or "-")
            content = " ".join(str(message.get("content") or "").split())
            message_task_ids = _message_task_ids(message)
            extra = f" / tasks {' '.join(f'#{task_id}' for task_id in message_task_ids)}" if message_task_ids else ""
            lines.append(f"- **{role}**：{content[:100]}{extra}")
        blocks.extend([_section("最近消息"), _md_block("\n".join(lines))])
    if related_task_ids:
        blocks.extend([_section("相关任务"), _plain_block("、".join(f"#{task_id}" for task_id in related_task_ids))])
    blocks.extend(_command_panel(prefix, _session_followup_commands(session_id, related_task_ids=related_task_ids)))
    return _card(f"需求会话详情 · #{session_id}", blocks, template="violet", subtitle="需求会话的状态和最近消息。")


def build_task_card(task_id: int, *, prefix: str = "", title_prefix: str = "任务详情") -> dict[str, Any]:
    task = task_detail_payload(task_id)
    latest = str(task.get("latest") or task.get("delivery_record") or task.get("error_message") or "").strip()
    latest = latest[:240]
    runtime = str(task.get("runtime") or "-")
    blocks: list[str | dict[str, Any]] = [
        _section("任务信息"),
        *_column_panels(
            [
                f"**项目**\n`{task['project']}`",
                f"**状态**\n`{_status_mark(task['status'])}`",
                f"**优先级**\n`{_priority_badge(task['priority'])}`",
                f"**Agent**\n`{task['agent']}`",
                f"**运行时长**\n`{runtime}`",
                f"**重试**\n`{int(task.get('retry_count') or 0)}/{int(task.get('max_retries') or 0)}`",
            ]
        ),
        _section("标题"),
        _plain_block(task["title"]),
    ]
    if latest:
        blocks.extend([_section("最近输出"), _plain_block(latest)])
    blocks.extend(_command_panel(prefix, _card_commands("task", task_id=task_id)))
    return _card(
        f"{title_prefix} · #{task_id}",
        blocks,
        template="green" if task["status"] == "done" else "blue",
        subtitle="任务详情和最相关的操作命令。",
    )


def build_task_log_card(task_id: int, *, prefix: str = "") -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    detail = task_detail_payload(task_id)
    text = str(detail.get("log_text") or _compose_log_text(task) or "").strip()
    preview = text[-1800:] if text else ""
    blocks: list[str | dict[str, Any]] = [
        *_section_note("日志摘要", "展示任务阶段和最近日志片段。"),
        *_column_panels(
            [
                f"**项目**\n`{detail.get('project') or '-'}`",
                f"**状态**\n`{_status_mark(detail.get('status') or '')}`",
                f"**当前阶段**\n`{_phase_label(detail.get('phase') or '-')}`",
            ]
        ),
        _section("标题"),
        _plain_block(detail.get("title") or ""),
    ]
    if preview:
        blocks.extend(_code_block(preview, title="日志 / 最近输出"))
    else:
        blocks.extend([_section("日志 / 最近输出"), _plain_block("当前没有可展示的日志。")])
    blocks.extend(_command_panel(prefix, _card_commands("task", task_id=task_id)))
    return _card(f"任务日志 · #{task_id}", blocks, template="grey", subtitle="代码块只展示最近片段，避免刷屏。")


def build_service_card(project_name: str, result: dict[str, Any], *, prefix: str = "", title: str = "任务执行服务") -> dict[str, Any]:
    status = result.get("status") if isinstance(result.get("status"), dict) else result
    running = bool(status.get("running"))
    stopping = bool(status.get("stopping"))
    started_at = str(status.get("started_at") or "").strip()
    log_path = str(status.get("log") or "").strip()
    blocks: list[str | dict[str, Any]] = [
        *_section_note("服务状态", "服务命令会影响本机后台进程。"),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**服务状态**\n`{_service_mark(status)}`",
                f"**PID**\n`{status.get('pid') or 0}`",
                f"**启动时间**\n`{started_at or '-'}`",
            ]
        )
    ]
    message = str(result.get("message") or "").strip()
    if message:
        blocks.extend([_section("结果"), _plain_block(message)])
    if stopping:
        blocks.append(_note("当前已收到停止请求，正在等待正在执行的任务收尾。"))
    if log_path:
        blocks.extend([_section("日志"), _plain_block(log_path)])
    blocks.extend(_command_panel(prefix, _card_commands("services", project=project_name)))
    return _card(f"{title} · {project_name}", blocks, template="purple", subtitle="服务控制结果和下一步命令。")


def build_services_card(project_name: str, *, prefix: str = "") -> dict[str, Any]:
    task_status = _project_service_status(project_name, "tasks")
    inspect_status = _project_service_status(project_name, "inspect")
    blocks: list[str | dict[str, Any]] = [
        _field_block(
            [
                _field(f"**任务轮询**\n`{_running_label(task_status)}`"),
                _field(f"**轮询 PID**\n`{task_status.get('pid') or 0}`"),
                _field(f"**巡检**\n`{_running_label(inspect_status)}`"),
                _field(f"**巡检 PID**\n`{inspect_status.get('pid') or 0}`"),
                _field(f"**轮询启动时间**\n`{task_status.get('started_at') or '-'}`"),
                _field(f"**巡检启动时间**\n`{inspect_status.get('started_at') or '-'}`"),
            ]
        ),
    ]
    blocks.extend(_command_panel(prefix, _card_commands("services", project=project_name)))
    return _card(
        f"项目服务状态 · {project_name}",
        blocks,
        template="purple",
        subtitle="任务轮询和巡检服务的当前运行状态。",
    )


def _question_lines(questions: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    multi_question = len(questions or []) > 1
    for idx, question in enumerate(questions or [], 1):
        text = str(question.get("text") or question.get("question") or "").strip()
        if not text:
            continue
        qtype = str(question.get("type") or "text").strip().lower()
        options = question.get("options") if isinstance(question.get("options"), list) else []
        if qtype == "single" and options:
            format_hint = f"答 {idx}=1" if multi_question else "答 1"
            lines.append(f"{idx}. [单选] {text}")
            lines.append(f"   回复格式：{format_hint}")
        elif qtype == "multi" and options:
            format_hint = f"答 {idx}=1,2" if multi_question else "答 1,2"
            lines.append(f"{idx}. [多选] {text}")
            lines.append(f"   回复格式：{format_hint}")
        else:
            format_hint = f"答 {idx}=你的说明" if multi_question else "答 你的说明"
            lines.append(f"{idx}. [文本] {text}")
            lines.append(f"   回复格式：{format_hint}")
        for option_idx, option in enumerate(options, 1):
            label = str((option or {}).get("label") or (option or {}).get("text") or "").strip()
            if label:
                lines.append(f"   {option_idx}) {label}")
        if question.get("allow_free_text"):
            lines.append("   其他：可直接文字回答")
    if multi_question:
        lines.append("")
        lines.append("多题一起回复示例：答 1=1,2; 2=补充说明")
    return "\n".join(lines) or "请补充更多信息。"


def _question_id(question: dict[str, Any], idx: int) -> str:
    return str(question.get("id") or question.get("question_id") or f"q{idx + 1}").strip()


def _option_id(option: dict[str, Any], idx: int) -> str:
    return str(option.get("id") or option.get("value") or f"opt{idx + 1}").strip()


def _choice_tokens(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、\s]+", str(raw or "").strip()) if item.strip()]


def _choice_answer_for_question(question: dict[str, Any], raw: str, *, q_index: int) -> dict[str, Any]:
    qtype = str(question.get("type") or "text").strip().lower()
    qid = _question_id(question, q_index)
    if qtype not in {"single", "multi"}:
        return {"question_id": qid, "selected_option_ids": [], "free_text": str(raw or "").strip()}

    options = question.get("options") if isinstance(question.get("options"), list) else []
    option_ids = [_option_id(option or {}, idx) for idx, option in enumerate(options)]
    selected: list[str] = []
    unmatched: list[str] = []
    for token in _choice_tokens(raw):
        matched = ""
        if token.isdigit():
            index = int(token) - 1
            if 0 <= index < len(option_ids):
                matched = option_ids[index]
        if not matched:
            for idx, option in enumerate(options):
                label = str((option or {}).get("label") or (option or {}).get("text") or "").strip()
                oid = option_ids[idx]
                if token == oid or (label and token == label):
                    matched = oid
                    break
        if matched and matched not in selected:
            selected.append(matched)
        elif token:
            unmatched.append(token)

    free_text = " ".join(unmatched).strip()
    if qtype == "single" and len(selected) > 1:
        raise RuntimeError(f"第 {q_index + 1} 题是单选，只能回复一个编号，例如 `答 1`。")
    if not selected and free_text and not question.get("allow_free_text"):
        raise RuntimeError(f"第 {q_index + 1} 题需要按选项编号回复，例如 `答 1` 或 `答 1,2`。")
    if selected and free_text and not question.get("allow_free_text"):
        raise RuntimeError(f"第 {q_index + 1} 题包含无法识别的选项：{free_text}")
    return {"question_id": qid, "selected_option_ids": selected, "free_text": free_text}


def _split_numbered_answers(raw: str) -> dict[int, str]:
    text = str(raw or "").strip()
    matches = list(re.finditer(r"(?:^|[;\n])\s*(\d+)\s*[:：=]\s*", text))
    if not matches:
        return {}
    result: dict[int, str] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        result[int(match.group(1)) - 1] = text[start:end].strip(" ;\n")
    return result


def _parse_feishu_clarify_answers(raw: str, questions: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    normalized_questions = [q for q in (questions or []) if isinstance(q, dict)]
    content = str(raw or "").strip()
    if not normalized_questions:
        return content, []
    if len(normalized_questions) == 1:
        return "", [_choice_answer_for_question(normalized_questions[0], content, q_index=0)]

    numbered = _split_numbered_answers(content)
    if not numbered:
        raise RuntimeError("多题澄清请按编号回复，例如 `答 1=1,2; 2=补充说明`。")
    answers: list[dict[str, Any]] = []
    for idx, question in enumerate(normalized_questions):
        if idx not in numbered:
            continue
        answers.append(_choice_answer_for_question(question, numbered[idx], q_index=idx))
    if not answers:
        raise RuntimeError("没有解析到有效答案，请按示例回复：`答 1=1,2; 2=补充说明`。")
    return "", answers


def build_requirement_clarify_card(
    project_name: str,
    result: dict[str, Any],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    seed = str(result.get("original_title") or result.get("seed_title") or "").strip()
    questions = result.get("questions") if isinstance(result.get("questions"), list) else []
    blocks: list[str | dict[str, Any]] = [
        _section("需求需要补充信息"),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**状态**\n`等待你回复`",
                f"**问题数**\n`{len(questions)}`",
            ]
        ),
    ]
    if seed:
        blocks.extend([_section("原始需求"), _plain_block(seed[:500])])
    blocks.extend(_code_block(_question_lines(questions), title="请直接回复答案"))
    blocks.append(_note("回复澄清请发送：答 <你的补充信息>。发送新的 需求 <内容> 会开始新需求。"))
    blocks.extend(_command_panel(prefix, _card_commands("project", project=project_name)))
    return _card(
        f"需求澄清 · {project_name}",
        blocks,
        template="orange",
        subtitle="AI 规划前需要补充关键信息。",
    )


def build_requirement_result_card(
    project_name: str,
    requirement_text: str,
    result: dict[str, Any],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    if result.get("intent") == "clarify":
        return build_requirement_clarify_card(project_name, result, prefix=prefix)
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    task_ids = job.get("task_ids") if isinstance(job.get("task_ids"), list) else []
    task_text = "、".join(f"#{int(task_id)}" for task_id in task_ids if str(task_id).isdigit()) or "规划中"
    status = str(job.get("status") or result.get("status") or "queued")
    phase = str(job.get("phase") or "queued")
    blocks: list[str | dict[str, Any]] = [
        _section("需求已进入规划"),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**后台任务**\n`#{job.get('id') or '-'}`",
                f"**状态**\n`{_status_mark(status)}`",
                f"**阶段**\n`{_phase_label(phase)}`",
                f"**关联任务**\n`{task_text}`",
            ]
        ),
        _section("需求内容"),
        _plain_block(str(requirement_text or "").strip()[:700]),
    ]
    message = str(result.get("message") or "").strip()
    if message:
        blocks.extend([_section("系统反馈"), _plain_block(message[:500])])
    blocks.extend(
        _command_panel(
            prefix,
            [
                ("requirements", "查看需求进度"),
                ("tasks", "查看任务面板"),
                ("services", "服务状态"),
                ("overview", "项目总览"),
            ],
        )
    )
    return _card(
        f"需求已提交 · {project_name}",
        blocks,
        template="green",
        subtitle="AI 已开始规划，稍后可在需求会话或任务面板查看进度。",
    )


def build_requirement_event_card(
    project_name: str,
    requirement_text: str,
    *,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    prefix: str = "",
) -> dict[str, Any]:
    title_map = {
        "started": "需求规划开始",
        "phase_start": f"{_phase_label(phase)} 开始",
        "phase_end": f"{_phase_label(phase)} 完成",
        "failed": "需求规划失败",
        "done": "需求规划完成",
    }
    template = "red" if level == "error" or event == "failed" else ("green" if event in {"phase_end", "done"} else "blue")
    blocks: list[str | dict[str, Any]] = [
        _section("规划进度"),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**事件**\n`{title_map.get(event, '需求状态更新')}`",
                f"**阶段**\n`{_phase_label(phase or '-')}`",
            ]
        ),
        _section("需求"),
        _plain_block(str(requirement_text or "").strip()[:500]),
    ]
    if message:
        blocks.extend([_section("当前反馈"), _plain_block(str(message or "").strip()[:700])])
    blocks.extend(
        _command_panel(
            prefix,
            [
                ("requirements", "查看需求进度"),
                ("tasks", "查看任务面板"),
                ("overview", "项目总览"),
            ],
        )
    )
    return _card(
        f"CodePilot · {title_map.get(event, '需求状态更新')}",
        blocks,
        template=template,
        subtitle="需求规划和任务生成进度通知。",
    )


def _send_requirement_event_card(
    project_name: str,
    chat_id: str,
    requirement_text: str,
    *,
    event: str,
    phase: str = "",
    level: str = "info",
    message: str = "",
    prefix: str = "",
) -> bool:
    if not chat_id:
        return False
    card = build_requirement_event_card(
        project_name,
        requirement_text,
        event=event,
        phase=phase,
        level=level,
        message=message,
        prefix=prefix,
    )
    return _send_bot_card(card, project_name=project_name, chat_ids=[chat_id])


def _submit_requirement_from_feishu(
    project_name: str,
    text: str,
    *,
    cfg: FeishuBotConfig,
    chat_id: str = "",
    continue_pending: bool = False,
) -> dict[str, Any]:
    content = str(text or "").strip()
    if not content:
        return _reply_card(build_help_card(prefix=cfg.command_prefix))
    from codepilot.core import progress_bus
    from codepilot.webapp import server as _web_server  # noqa: F401 - initializes Web UI action state module
    from codepilot.webapp import actions as web_actions

    pending = _load_pending_requirement(chat_id, project_name) if continue_pending else None

    def _progress_listener(event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "").strip()
        if event_type not in {"phase_start", "phase_end", "error"}:
            return
        _send_requirement_event_card(
            project_name,
            chat_id,
            content,
            event=event_type,
            phase=str(event.get("stage") or ""),
            level=str(event.get("level") or "info"),
            message=str(event.get("message") or ""),
            prefix=cfg.command_prefix,
        )

    if pending:
        answer_text, clarify_answers = _parse_feishu_clarify_answers(
            content,
            pending.get("questions") if isinstance(pending.get("questions"), list) else [],
        )
        with progress_bus.subscription(_progress_listener):
            result = web_actions.submit_requirement_action(
                project_name,
                answer_text,
                execute=True,
                run_async=False,
                task_source=f"feishu:{chat_id}" if chat_id else "feishu",
                qa_history=pending.get("qa_history") if isinstance(pending.get("qa_history"), list) else [],
                original_title=str(pending.get("original_title") or ""),
                clarify_questions=pending.get("questions") if isinstance(pending.get("questions"), list) else [],
                clarify_answers=clarify_answers,
            )
        if result.get("intent") == "clarify":
            _save_pending_requirement(chat_id, project_name, result)
        else:
            _clear_pending_requirement(chat_id)
        return _reply_card(build_requirement_result_card(project_name, content, result, prefix=cfg.command_prefix))

    if not continue_pending:
        _clear_pending_requirement(chat_id)
    _send_requirement_event_card(
        project_name,
        chat_id,
        content,
        event="started",
        phase="planning",
        level="info",
        message="已收到需求，开始澄清/规划。",
        prefix=cfg.command_prefix,
    )
    with progress_bus.subscription(_progress_listener):
        result = web_actions.submit_requirement_action(
            project_name,
            content,
            execute=True,
            run_async=False,
            task_source=f"feishu:{chat_id}" if chat_id else "feishu",
        )
    if result.get("intent") == "clarify":
        _save_pending_requirement(chat_id, project_name, result)
    else:
        _clear_pending_requirement(chat_id)
    return _reply_card(build_requirement_result_card(project_name, content, result, prefix=cfg.command_prefix))


def _start_session_from_feishu(
    project_name: str,
    user_text: str,
    *,
    category: str = "requirement",
    chat_id: str = "",
    prefix: str = "",
) -> dict[str, Any]:
    content = str(user_text or "").strip()
    if not content:
        raise RuntimeError("输入不能为空。")
    from codepilot.webapp import server as _web_server  # noqa: F401 - initializes Web UI action state module
    from codepilot.webapp import actions as web_actions

    session_result = web_actions.create_session_action(project_name, title=content[:40] or "新会话")
    session = session_result.get("session") if isinstance(session_result.get("session"), dict) else {}
    session_id = int(session.get("id") or 0)
    if session_id <= 0:
        raise RuntimeError("创建需求会话失败。")
    result = web_actions.send_session_message_action(session_id, content, category=category)
    if chat_id and project_name:
        _save_chat_project(chat_id, project_name)
    _clear_pending_requirement(chat_id)
    _clear_pending_goal_text(chat_id)
    return _reply_card(build_session_result_card(session_id, content, result, prefix=prefix))


def _continue_session_from_feishu(
    session_id: int,
    user_text: str,
    *,
    chat_id: str = "",
    prefix: str = "",
) -> dict[str, Any]:
    content = str(user_text or "").strip()
    if not content:
        raise RuntimeError("输入不能为空。")
    from codepilot.webapp import server as _web_server  # noqa: F401 - initializes Web UI action state module
    from codepilot.webapp import actions as web_actions

    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    result = web_actions.send_session_message_action(session_id, content, category="auto")
    if chat_id and str(session.get("project") or "").strip():
        _save_chat_project(chat_id, str(session.get("project") or "").strip())
    _clear_pending_requirement(chat_id)
    _clear_pending_goal_text(chat_id)
    return _reply_card(build_session_result_card(session_id, content, result, prefix=prefix))


def _feishu_notify_script() -> Path:
    return Path(__file__).resolve().parent / "feishu_notify.mjs"


def _send_bot_card(card: dict[str, Any], *, project_name: str = "", chat_ids: list[str] | None = None) -> bool:
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
                f"**状态**\n`{_status_mark(status) if status else '-'}`",
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
                "sent_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    except Exception:
        pass


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
    sent = _send_bot_card(card, project_name=resolved_project, chat_ids=chat_ids)
    if sent:
        _mark_feishu_notification_sent(
            dedupe_scope,
            event=event,
            task_id=int(task_id),
            project_name=resolved_project,
        )
    return sent


def _normalize_command_text(text: str, prefix: str) -> str | None:
    content = " ".join(str(text or "").strip().split())
    if not content:
        return None
    if prefix:
        if not content.lower().startswith(prefix.lower()):
            return None
        content = content[len(prefix):].strip()
        if not content:
            return "help"
    return content


def _parse_task_id(token: str) -> int:
    try:
        task_id = int(str(token or "").strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("任务 ID 必须是整数。") from exc
    if task_id <= 0:
        raise RuntimeError("任务 ID 必须大于 0。")
    return task_id


def _parse_task_ids(tokens: list[str], *, command_name: str) -> list[int]:
    raw_parts = [str(token or "").strip() for token in tokens if str(token or "").strip()]
    if not raw_parts:
        raise RuntimeError(f"请提供任务 ID，例如 `{command_name} 123`。")
    raw_items = [item for item in re.split(r"[\s,，]+", " ".join(raw_parts)) if item]
    if not raw_items:
        raise RuntimeError(f"请提供任务 ID，例如 `{command_name} 123`。")

    task_ids: list[int] = []
    seen: set[int] = set()
    for item in raw_items:
        task_id = _parse_task_id(item)
        if task_id in seen:
            continue
        seen.add(task_id)
        task_ids.append(task_id)
    return task_ids


def _batch_action_label(action: str) -> str:
    return {
        "cancel": "取消",
        "archive": "归档",
        "delete": "删除",
    }.get(str(action or "").strip().lower(), str(action or "").strip())


def build_batch_task_action_card(action: str, result: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    action_label = _batch_action_label(action_key)
    first_task_id = 0
    for item in result.get("succeeded") or []:
        task = item.get("task") if isinstance(item, dict) else None
        if isinstance(task, dict) and int(task.get("id") or 0) > 0:
            first_task_id = int(task["id"])
            break

    blocks: list[str | dict[str, Any]] = [
        _field_block(
            [
                _field(f"**操作**\n`批量{action_label}`"),
                _field(f"**总计**\n`{int(result.get('total') or 0)}`"),
                _field(f"**成功**\n`{int(result.get('success_count') or 0)}`"),
                _field(f"**失败**\n`{int(result.get('failed_count') or 0)}`"),
            ]
        ),
        _note(str(result.get("message") or "").strip()),
    ]

    succeeded = result.get("succeeded") or []
    if succeeded:
        success_lines: list[str] = []
        for item in succeeded[:8]:
            if not isinstance(item, dict):
                continue
            task = item.get("task") if isinstance(item.get("task"), dict) else None
            task_id = int((task or {}).get("id") or item.get("deleted_task_id") or item.get("task_id") or 0)
            if task_id <= 0:
                continue
            success_lines.append(f"- `#{task_id}` {str(item.get('message') or '').strip()}")
        if success_lines:
            blocks.extend([*_section_note("成功任务"), _md_block("\n".join(success_lines))])

    failed = result.get("failed") or []
    if failed:
        failed_lines: list[str] = []
        for item in failed[:8]:
            if not isinstance(item, dict):
                continue
            task_id = int(item.get("task_id") or 0)
            error = str(item.get("error") or "").strip()
            if task_id <= 0:
                continue
            failed_lines.append(f"- `#{task_id}` {error}")
        if failed_lines:
            blocks.extend([*_section_note("失败任务"), _md_block("\n".join(failed_lines))])

    commands: list[tuple[str, str]] = [("tasks", "任务面板")]
    if first_task_id and action_key != "delete":
        commands.insert(0, (f"detail {first_task_id}", "查看首个成功任务"))
        commands.insert(1, (f"logs {first_task_id}", "查看首个任务日志"))
    blocks.extend(_command_panel(prefix, commands))
    return _card(
        f"批量{action_label}完成",
        blocks,
        template="green" if int(result.get("failed_count") or 0) == 0 else "orange",
        subtitle="飞书已执行批量任务操作，并返回逐项结果。",
    )


def handle_command_text(
    text: str,
    *,
    config_path: Path | None = None,
    chat_id: str = "",
    _normalized_command_text: str | None = None,
    _allow_nl: bool = True,
) -> dict[str, Any]:
    db.init_db()
    cfg = load_feishu_bot_config(config_path)
    command_text = _normalized_command_text
    if command_text is None:
        command_text = _normalize_command_text(text, cfg.command_prefix)
    if command_text is None:
        return {"type": "ignore"}
    pending_options = _load_pending_action_options(chat_id)
    selected = pick_command_option(command_text, pending_options)
    if selected:
        _clear_pending_action_options(chat_id)
        selected_command = str(selected.get("command") or "")
        pending_goal_text = _load_pending_goal_text(chat_id)
        if pending_goal_text and selected_command.lower().startswith("use "):
            project_name = selected_command.split(None, 1)[1].strip()
            return _submit_goal_from_feishu(
                project_name,
                pending_goal_text,
                chat_id=chat_id,
                prefix=cfg.command_prefix,
            )
        return handle_command_text(
            selected_command,
            config_path=config_path,
            chat_id=chat_id,
            _normalized_command_text=selected_command,
            _allow_nl=False,
        )
    if pending_options and str(command_text).isdigit():
        return _reply_card(build_choice_card("可选项超出范围，请重新选择：", pending_options, prefix=cfg.command_prefix))
    if pending_options:
        _clear_pending_action_options(chat_id)
        _clear_pending_goal_text(chat_id)
    parts = command_text.split()
    if not parts:
        return _reply_card(build_help_card(prefix=cfg.command_prefix))

    verb = parts[0].lower()
    if verb in {"help", "h", "?"}:
        return _reply_card(build_help_card(prefix=cfg.command_prefix))
    if verb in {"global", "summary", "all"}:
        return _reply_card(
            build_global_status_card(prefix=cfg.command_prefix, default_project=_active_project(cfg, chat_id))
        )
    if verb in {"use", "project"}:
        if len(parts) < 2:
            active = _active_project(cfg, chat_id)
            if active:
                return _reply_card(build_overview_card(active, prefix=cfg.command_prefix))
            return _reply_card(build_projects_card(prefix=cfg.command_prefix, default_project=active))
        project_name = _resolve_project(parts[1], default_project=_active_project(cfg, chat_id))
        if chat_id:
            _save_chat_project(chat_id, project_name)
        return _reply_card(
            _card(
                f"已切换项目 · {project_name}",
                [
                    _field_block(
                        [
                            _field(f"**当前项目**\n`{project_name}`"),
                            _field("**会话状态**\n`已进入项目`"),
                        ]
                    ),
                    *_command_panel(cfg.command_prefix, _card_commands("project", project=project_name)),
                ],
                template="green",
            )
        )
    if verb in {"overview", "ov"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=_active_project(cfg, chat_id))
        return _reply_card(build_overview_card(project_name, prefix=cfg.command_prefix))
    if verb in {"projects", "ls"}:
        return _reply_card(build_projects_card(prefix=cfg.command_prefix, default_project=_active_project(cfg, chat_id)))
    if verb in {"tasks", "panel"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=_active_project(cfg, chat_id))
        return _reply_card(build_tasks_card(project_name, prefix=cfg.command_prefix))
    if verb in {"requirements", "sessions", "jobs"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=_active_project(cfg, chat_id))
        return _reply_card(build_sessions_card(project_name, prefix=cfg.command_prefix))
    if verb == "req":
        if len(parts) > 1 and parts[1].lower() == "new":
            requirement_text = command_text.split(None, 2)[2].strip() if len(parts) > 2 else ""
        else:
            requirement_text = command_text[len(parts[0]):].strip()
        if not requirement_text:
            raise RuntimeError("请在命令后写需求内容，例如 `req new 优化飞书任务卡片`。")
        project_name = _resolve_project("", default_project=_active_project(cfg, chat_id))
        return _start_session_from_feishu(
            project_name,
            requirement_text,
            category="requirement",
            chat_id=chat_id,
            prefix=cfg.command_prefix,
        )
    if verb == "ask":
        user_text = command_text[len(parts[0]):].strip()
        if not user_text:
            raise RuntimeError("请在命令后写内容，例如 `ask 帮我梳理一下最近需求`。")
        project_name = _resolve_project("", default_project=_active_project(cfg, chat_id))
        return _start_session_from_feishu(
            project_name,
            user_text,
            category="auto",
            chat_id=chat_id,
            prefix=cfg.command_prefix,
        )
    if verb in {"需求", "requirement", "plan", "new", "goal"}:
        requirement_text = command_text[len(parts[0]):].strip()
        if not requirement_text:
            raise RuntimeError("请在命令后写需求内容，例如 `需求 优化任务面板状态展示`。")
        project_name = _resolve_project("", default_project=_active_project(cfg, chat_id))
        return _submit_requirement_from_feishu(
            project_name,
            requirement_text,
            cfg=cfg,
            chat_id=chat_id,
            continue_pending=False,
        )
    if verb in {"答", "answer", "reply"}:
        answer_text = command_text[len(parts[0]):].strip()
        if not answer_text:
            raise RuntimeError("请在命令后写补充答案，例如 `答 先做飞书控制入口`。")
        project_name = _resolve_project("", default_project=_active_project(cfg, chat_id))
        if not _load_pending_requirement(chat_id, project_name):
            raise RuntimeError("当前没有等待补充的需求。请先发送 `需求 <内容>`。")
        return _submit_requirement_from_feishu(
            project_name,
            answer_text,
            cfg=cfg,
            chat_id=chat_id,
            continue_pending=True,
        )
    if verb in {"session", "req", "job"}:
        if verb == "session" and len(parts) > 1 and parts[1].lower() in {"reply", "continue"}:
            if len(parts) < 4:
                raise RuntimeError("请提供会话 ID 和内容，例如 `session reply 12 先做飞书入口`。")
            session_id = _parse_task_id(parts[2])
            reply_text = command_text.split(None, 3)[3].strip() if len(parts) > 3 else ""
            if not reply_text:
                raise RuntimeError("请提供会话回复内容，例如 `session reply 12 先做飞书入口`。")
            return _continue_session_from_feishu(
                session_id,
                reply_text,
                chat_id=chat_id,
                prefix=cfg.command_prefix,
            )
        if len(parts) < 2:
            raise RuntimeError("请提供会话 ID，例如 `session 12`。")
        return _reply_card(build_session_card(_parse_task_id(parts[1]), prefix=cfg.command_prefix))
    if verb in {"detail", "task", "show"}:
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `detail 123`。")
        return _reply_card(build_task_card(_parse_task_id(parts[1]), prefix=cfg.command_prefix))
    if verb in {"logs", "log"}:
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `logs 123`。")
        return _reply_card(build_task_log_card(_parse_task_id(parts[1]), prefix=cfg.command_prefix))
    if verb == "stop":
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `stop 123`。")
        task_id = _parse_task_id(parts[1])
        stop_task_action(task_id)
        return _reply_card(build_task_card(task_id, prefix=cfg.command_prefix, title_prefix="停止请求已发送"))
    if verb in {"cancel", "discard"}:
        task_ids = _parse_task_ids(parts[1:], command_name="cancel")
        if len(task_ids) > 1:
            return _reply_card(
                build_batch_task_action_card(
                    "cancel",
                    batch_task_action(task_ids, "cancel"),
                    prefix=cfg.command_prefix,
                )
            )
        task_id = task_ids[0]
        cancel_task_action(task_id)
        return _reply_card(build_task_card(task_id, prefix=cfg.command_prefix, title_prefix="任务已取消"))
    if verb in {"archive", "arch"}:
        task_ids = _parse_task_ids(parts[1:], command_name="archive")
        if len(task_ids) > 1:
            return _reply_card(
                build_batch_task_action_card(
                    "archive",
                    batch_task_action(task_ids, "archive"),
                    prefix=cfg.command_prefix,
                )
            )
        task_id = task_ids[0]
        archive_task_action(task_id)
        return _reply_card(build_task_card(task_id, prefix=cfg.command_prefix, title_prefix="任务已归档"))
    if verb in {"delete", "del", "rm"}:
        task_ids = _parse_task_ids(parts[1:], command_name="delete")
        if len(task_ids) > 1:
            return _reply_card(
                build_batch_task_action_card(
                    "delete",
                    batch_task_action(task_ids, "delete"),
                    prefix=cfg.command_prefix,
                )
            )
        task_id = task_ids[0]
        delete_task_action(task_id)
        return _reply_card(
            _card(
                f"任务已删除 · #{task_id}",
                [
                    _field_block(
                        [
                            _field(f"**任务 ID**\n`#{task_id}`"),
                            _field("**结果**\n`已删除`"),
                        ]
                    ),
                    *_command_panel(cfg.command_prefix, _card_commands("tasks")),
                ],
                template="green",
            )
        )
    if verb == "retry":
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `retry 123`。")
        task_id = _parse_task_id(parts[1])
        retry_task_action(task_id)
        return _reply_card(build_task_card(task_id, prefix=cfg.command_prefix, title_prefix="任务已重试"))
    if verb in {"run", "start"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=_active_project(cfg, chat_id))
        result = project_service_action(project_name, "tasks", "start")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title="任务执行服务已启动"))
    if verb in {"daemon", "worker"}:
        action = parts[1].lower() if len(parts) > 1 else "status"
        project_name = _resolve_project(parts[2] if len(parts) > 2 else "", default_project=_active_project(cfg, chat_id))
        result = project_service_action(project_name, "tasks", action)
        title = {
            "start": "任务轮询已启动",
            "stop": "任务轮询停止请求",
            "status": "任务轮询状态",
        }.get(action, "任务轮询状态")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title=title))
    if verb == "inspect":
        action = parts[1].lower() if len(parts) > 1 else "status"
        project_name = _resolve_project(parts[2] if len(parts) > 2 else "", default_project=_active_project(cfg, chat_id))
        result = project_service_action(project_name, "inspect", action)
        title = {
            "start": "巡检服务已启动",
            "stop": "巡检服务停止请求",
            "status": "巡检服务状态",
        }.get(action, "巡检服务状态")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title=title))
    if verb in {"services", "service"}:
        if len(parts) > 1 and parts[1].lower() in {"all", "global"}:
            return _reply_card(
                build_global_status_card(prefix=cfg.command_prefix, default_project=_active_project(cfg, chat_id))
            )
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=_active_project(cfg, chat_id))
        return _reply_card(build_services_card(project_name, prefix=cfg.command_prefix))
    if verb == "status":
        if len(parts) > 1 and parts[1].lower() in {"all", "global"}:
            return _reply_card(
                build_global_status_card(prefix=cfg.command_prefix, default_project=_active_project(cfg, chat_id))
            )
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=_active_project(cfg, chat_id))
        result = project_service_action(project_name, "tasks", "status")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title="任务执行服务状态"))

    if _allow_nl:
        active_project = _active_project(cfg, chat_id)
        resolved = resolve_natural_language_command(command_text, active_project=active_project)
        if resolved.get("status") == "options":
            options = list(resolved.get("options") or [])
            _save_pending_action_options(chat_id, options)
            return _reply_card(build_choice_card(str(resolved.get("message") or "请确认操作"), options, prefix=cfg.command_prefix))
        if resolved.get("status") == "match":
            _clear_pending_action_options(chat_id)
            return handle_command_text(
                str(resolved.get("command") or ""),
                config_path=config_path,
                chat_id=chat_id,
                _normalized_command_text=str(resolved.get("command") or ""),
                _allow_nl=False,
            )

        goal = infer_goal_from_text(command_text, active_project=active_project)
        if goal and goal.get("status") == "options":
            options = list(goal.get("options") or [])
            _save_pending_action_options(chat_id, options)
            _save_pending_goal_text(chat_id, command_text)
            return _reply_card(build_choice_card(str(goal.get("message") or "请确认项目"), options, prefix=cfg.command_prefix))
        if goal and goal.get("project"):
            return _submit_goal_from_feishu(
                str(goal.get("project") or "").strip(),
                str(goal.get("text") or "").strip(),
                chat_id=chat_id,
                prefix=cfg.command_prefix,
            )

        fallback_project = active_project
        if not fallback_project:
            projects = db.list_projects()
            if len(projects) == 1:
                fallback_project = str(projects[0].get("name") or "").strip()
            elif len(projects) > 1:
                options = [
                    {"command": f"use {str(project.get('name') or '').strip()}", "label": f"在项目 {str(project.get('name') or '').strip()} 中继续"}
                    for project in projects[:4]
                    if str(project.get("name") or "").strip()
                ]
                if options:
                    _save_pending_action_options(chat_id, options)
                    _save_pending_goal_text(chat_id, command_text)
                    return _reply_card(build_choice_card("这条消息会按 chat 处理。先确认你要在哪个项目里继续：", options, prefix=cfg.command_prefix))
        if fallback_project:
            return _submit_goal_from_feishu(
                fallback_project,
                command_text,
                chat_id=chat_id,
                prefix=cfg.command_prefix,
            )

    return _reply_card(build_help_card(prefix=cfg.command_prefix, error=f"`{command_text}`"))


def handle_event_payload(payload: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    try:
        _touch_chat_seen(str(payload.get("chat_id") or ""))
        return handle_command_text(
            str(payload.get("text") or ""),
            config_path=config_path,
            chat_id=str(payload.get("chat_id") or ""),
        )
    except Exception as exc:
        cfg = load_feishu_bot_config(config_path)
        return _reply_card(
            _card(
                "CodePilot 命令执行失败",
                [str(exc)],
                template="red",
                note=_help_note(cfg.command_prefix),
            )
        )
