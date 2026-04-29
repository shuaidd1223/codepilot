"""Feishu long-connection bot command routing and card rendering."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from codepilot.core.config import load_config
from codepilot.core.runtime import is_process_alive
from codepilot.nl_command_router import (
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
    create_project_action,
    delete_project_action,
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


@dataclass(frozen=True)
class _CommandContext:
    cfg: FeishuBotConfig
    config_path: Path | None = None
    chat_id: str = ""


_CHAT_CONTEXT_SERVICE = "feishu_chat"
_CHAT_CONTEXT_PREFIX = "chat:"
_NOTIFY_DEDUPE_SERVICE = "feishu_notify"
_INBOUND_DEDUPE_SERVICE = "feishu_inbound_msg"
_PENDING_REQUIREMENT_KEY = "pending_requirement"
_PENDING_ACTION_OPTIONS_KEY = "pending_action_options"
_PENDING_GOAL_TEXT_KEY = "pending_goal_text"
_PENDING_CONFIRM_SERVICE = "feishu_confirm"
_PENDING_CONFIRM_DIRECT_SCOPE = "__direct__"
_PENDING_CONFIRM_TTL_SECONDS = 120


def _inbound_dedupe_key(payload: dict[str, Any]) -> str:
    event_id = str(payload.get("event_id") or "").strip()
    if event_id:
        return f"event:{event_id}"
    message_id = str(payload.get("message_id") or "").strip()
    if message_id:
        return f"msg:{message_id}"
    return ""


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


_TASK_PANEL_PAGE_SIZE = 8
_TASK_STATUS_FILTER_ALIASES = {
    "all": "all",
    "active": "all",
    "open": "all",
    "全部": "all",
    "全部任务": "all",
    "全部状态": "all",
    "backlog": "backlog",
    "pending": "backlog",
    "todo": "backlog",
    "待执行": "backlog",
    "待办": "backlog",
    "in_progress": "in_progress",
    "running": "in_progress",
    "progress": "in_progress",
    "执行中": "in_progress",
    "进行中": "in_progress",
    "done": "done",
    "completed": "done",
    "完成": "done",
    "已完成": "done",
    "failed": "failed",
    "error": "failed",
    "失败": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "cancel": "cancelled",
    "stopped": "cancelled",
    "取消": "cancelled",
    "已取消": "cancelled",
    "archived": "archived",
    "archive": "archived",
    "归档": "archived",
    "已归档": "archived",
}
_TASK_STATUS_FILTER_ORDER = ("all", "in_progress", "backlog", "failed", "cancelled", "done", "archived")


def _md_block(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": str(content or "").strip()}}


def _plain_block(content: str) -> dict[str, Any]:
    return _md_block(str(content or "").strip())


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


def _is_section_block(block: dict[str, Any]) -> bool:
    text = block.get("text") if isinstance(block, dict) else None
    if not isinstance(text, dict) or text.get("tag") != "lark_md":
        return False
    content = str(text.get("content") or "").strip()
    return bool(content.startswith("**") and content.endswith("**") and "\n" not in content)


def _section_note(label: str, hint: str = "") -> list[dict[str, Any]]:
    blocks = [_section(label)]
    if hint:
        blocks.append(_note(hint))
    return blocks


def _status_mark(status: str) -> str:
    value = str(status or "").strip()
    label = _status_label(value)
    if value in {"done", "running", "in_progress"}:
        return f'<font color="green">OK · {label}</font>'
    if value in {"failed", "cancelled"}:
        return f'<font color="red">注意 · {label}</font>'
    if value in {"backlog", "pending"}:
        return f'<font color="grey">等待 · {label}</font>'
    if value in {"archived"}:
        return f'<font color="grey">归档 · {label}</font>'
    return label


def _service_mark(status: dict[str, Any]) -> str:
    if status.get("stopping"):
        return '<font color="red">停止中</font>'
    if status.get("running"):
        return '<font color="green">运行中</font>'
    return '<font color="grey">未运行</font>'


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


def _command_value(prefix: str, command: str) -> str:
    lead = f"{prefix} " if prefix else ""
    return f"{lead}{command}".strip()


def _button(label: str, command: str, *, prefix: str = "", button_type: str = "default") -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": str(label or "").strip()[:20]},
        "type": button_type,
        "value": {"command": _command_value(prefix, command)},
    }


def _button_row(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    actions = buttons[:4]
    block: dict[str, Any] = {"tag": "action", "actions": actions}
    if len(actions) > 1:
        block["layout"] = "flow"
    return block


def _button_rows(buttons: list[dict[str, Any]], *, per_row: int = 4) -> list[dict[str, Any]]:
    return [_button_row(buttons[index:index + per_row]) for index in range(0, len(buttons), per_row) if buttons[index:index + per_row]]


def _command_button_type(command: str) -> str:
    normalized = str(command or "").strip().lower()
    if normalized.startswith("cancel confirm"):
        return "default"
    if normalized.startswith(("confirm ", "delete ", "project delete ", "stop ", "cancel ")):
        return "danger"
    if normalized.startswith(("use ", "overview", "tasks", "projects", "global", "help")):
        return "primary"
    if normalized.startswith(("daemon start", "inspect start", "retry ", "resume ")):
        return "primary"
    return "default"


def _command_buttons(prefix: str, commands: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        _button(label, command, prefix=prefix, button_type=_command_button_type(command))
        for command, label in commands
        if str(command or "").strip()
    ]


def _command_action_blocks(prefix: str, commands: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return _button_rows(_command_buttons(prefix, commands))


def _option_field(option: Any, key: str) -> str:
    if isinstance(option, dict):
        return str(option.get(key) or "").strip()
    return str(getattr(option, key, "") or "").strip()


def _choice_action_blocks(prefix: str, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    for index, option in enumerate(options or [], 1):
        command = _option_field(option, "command")
        label = _option_field(option, "label") or command or f"选项 {index}"
        buttons.append(
            _button(
                f"{index}. {label}",
                str(index),
                prefix=prefix,
                button_type=_command_button_type(command),
            )
        )
    return _button_rows(buttons)


def _task_status_color(status: str) -> str:
    value = str(status or "").strip()
    if value in {"failed", "cancelled"}:
        return "red"
    if value in {"in_progress", "running", "done"}:
        return "green"
    if value in {"backlog", "pending", "archived"}:
        return "grey"
    return "grey"


def _task_action_buttons(task: dict[str, Any], *, prefix: str = "") -> list[dict[str, Any]]:
    task_id = int(task["id"])
    actions = task.get("actions") if isinstance(task.get("actions"), dict) else {}
    buttons = [
        _button("查看详情", f"detail {task_id}", prefix=prefix, button_type="primary"),
        _button("任务日志", f"logs {task_id}", prefix=prefix),
    ]
    if actions.get("stop"):
        buttons.append(_button("停止任务", f"stop {task_id}", prefix=prefix, button_type="danger"))
    elif actions.get("retry"):
        buttons.append(_button("重试任务", f"retry {task_id}", prefix=prefix))
    if actions.get("cancel"):
        buttons.append(_button("取消任务", f"cancel {task_id}", prefix=prefix, button_type="danger"))
    elif actions.get("archive"):
        buttons.append(_button("归档任务", f"archive {task_id}", prefix=prefix))
    if actions.get("delete"):
        buttons.append(_button("删除任务", f"delete {task_id}", prefix=prefix, button_type="danger"))
    return buttons


def _task_row_blocks(task: dict[str, Any], *, prefix: str = "") -> list[dict[str, Any]]:
    task_id = int(task["id"])
    status = str(task.get("status") or "")
    color = _task_status_color(status)
    phase = _phase_label(str(task.get("phase") or ""))
    runtime = str(task.get("runtime") or "").strip()
    latest = str(task.get("latest") or "").strip().replace("\n", " ")
    title = str(task.get("title") or "").strip()
    blocks: list[dict[str, Any]] = [
        _md_block(
            f"<font color=\"{color}\">● {_status_label(status)}</font> "
            f"{_priority_badge(str(task.get('priority') or 'P2'))} "
            f"任务 #{task_id} **{title[:72]}**"
        ),
        _column_panel(
            [
                f"**阶段**\n`{phase}`",
                f"**Agent**\n`{task.get('agent') or '-'}`",
                f"**重试**\n`{int(task.get('retry_count') or 0)}/{int(task.get('max_retries') or 0)}`",
                f"**运行**\n`{runtime or '-'}`",
            ],
            background="default",
        ),
        _md_block("**任务操作**"),
        _button_row(_task_action_buttons(task, prefix=prefix)),
    ]
    if latest:
        blocks.insert(
            2,
            _md_block(f"<font color=\"grey\">最近输出</font>\n{latest[:140]}"),
        )
    return blocks


def _command_panel(prefix: str, commands: list[tuple[str, str]], *, title: str = "快捷操作") -> list[dict[str, Any] | str]:
    if not commands:
        return []
    return [_hr(), *_section_note(title, "点击按钮直接执行；所有操作通过飞书卡片回调处理。"), *_command_action_blocks(prefix, commands)]


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
    has_body_section = False
    for block in blocks:
        if isinstance(block, dict):
            if (
                _is_section_block(block)
                and has_body_section
                and elements
                and elements[-1].get("tag") != "hr"
            ):
                elements.append(_hr())
            if _is_section_block(block):
                has_body_section = True
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
        "config": {"wide_screen_mode": True, "enable_forward": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title[:120]},
        },
        "elements": elements,
    }


def _reply_card(card: dict[str, Any]) -> dict[str, Any]:
    return {"type": "interactive", "card": card}


def _post_content(text: str, *, max_lines: int = 80) -> list[list[dict[str, str]]]:
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        lines = ["CodePilot 没有可展示的详情。"]
    return [[{"tag": "text", "text": line[:4000]}] for line in lines[:max_lines]]


def _reply_post(title: str, text: str) -> dict[str, Any]:
    return {
        "type": "post",
        "title": str(title or "CodePilot 详情").strip()[:120],
        "content": _post_content(text),
    }


def _reply_multi(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "multi", "messages": [message for message in messages if message and message.get("type") != "ignore"]}


def _help_note(prefix: str) -> str:
    lead = f"{prefix} " if prefix else ""
    return (
        f"命令示例: {lead}global | {lead}use demo | {lead}project info demo | {lead}project add demo D:\\work\\demo | "
        f"{lead}overview | {lead}tasks | {lead}requirements | "
        f"{lead}tasks demo status=failed page=2 | "
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
    value = value if value in {"P0", "P1", "P2", "P3"} else "P2"
    color = {"P0": "red", "P1": "red", "P2": "green", "P3": "grey"}.get(value, "grey")
    return f'<font color="{color}">{value}</font>'


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
    _clear_pending_confirm(chat_id)


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


def _confirm_scope(chat_id: str) -> str:
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


def build_pending_confirm_card(pending: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    token = str(pending.get("token") or "").strip().upper()
    expires_at = str(pending.get("expires_at") or "").strip() or "-"
    detail_lines = pending.get("details") if isinstance(pending.get("details"), list) else []
    blocks: list[str | dict[str, Any]] = [
        *_section_note("请二次确认", "敏感操作不会立即执行，必须在当前飞书会话中再次确认。"),
        _field_block(
            [
                _field(f"**操作**\n`{str(pending.get('summary') or '').strip() or '-'}`"),
                _field(f"**确认口令**\n`{token}`"),
                _field(f"**有效期**\n`{expires_at}`"),
            ]
        ),
    ]
    if detail_lines:
        blocks.extend([*_section_note("影响范围"), _md_block("\n".join(str(item).strip() for item in detail_lines if str(item).strip()))])
    blocks.extend(
        _command_panel(
            prefix,
            [
                (f"confirm {token}", "确认执行"),
                (f"cancel confirm {token}", "取消本次确认"),
            ],
            title="确认操作",
        )
    )
    return _card(
        "敏感操作待确认",
        blocks,
        template="orange",
        subtitle=f"确认口令 {token} 将在 {_PENDING_CONFIRM_TTL_SECONDS} 秒后失效。",
    )


def build_confirm_invalid_card(*, prefix: str = "", reason: str = "当前没有可执行的确认操作。") -> dict[str, Any]:
    return _card(
        "确认已失效",
        [
            _plain_block(reason),
            _note("请重新发送原始敏感命令以生成新的确认口令。"),
        ],
        template="red",
        subtitle="确认上下文不存在、已过期，或不属于当前飞书会话。",
    )


def build_confirm_cancelled_card(token: str, *, prefix: str = "") -> dict[str, Any]:
    return _card(
        f"确认已取消 · {token}",
        [
            _plain_block("本次敏感操作已取消，不会继续执行。"),
            _note("如需继续，请重新发送原始敏感命令。"),
        ],
        template="grey",
        subtitle="确认上下文已清理。",
    )


def build_project_deleted_card(result: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    project_name = str(result.get("project") or "").strip() or "-"
    deleted_tasks = int(result.get("deleted_tasks") or 0)
    path = str(result.get("path") or "").strip() or "-"
    return _card(
        f"项目已删除 · {project_name}",
        [
            _field_block(
                [
                    _field(f"**项目**\n`{project_name}`"),
                    _field(f"**关联任务**\n`{deleted_tasks}`"),
                    _field("**结果**\n`已删除注册记录`"),
                ]
            ),
            _md_block(f"工作目录保留：`{path}`"),
            *_command_panel(prefix, [("projects", "项目清单"), ("global", "全局状态")]),
        ],
        template="green",
        subtitle="项目注册记录和关联任务/会话已删除。",
    )


def build_project_command_error_card(
    title: str,
    error: str,
    *,
    prefix: str = "",
    commands: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    fallback_commands = commands or [("projects", "项目清单"), ("global", "全局状态"), ("help", "命令帮助")]
    return _card(
        title,
        [
            _plain_block(str(error or "").strip() or "项目命令执行失败。"),
            _note("只会管理本机已存在的项目目录；不会删除工作目录，也不会浏览远程文件系统。"),
            *_command_panel(prefix, fallback_commands),
        ],
        template="red",
        subtitle="项目管理命令未执行成功。",
    )


def _execute_pending_confirm(pending: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    action = str(pending.get("action") or "").strip().lower()
    if action == "delete_task":
        task_ids = pending.get("task_ids") if isinstance(pending.get("task_ids"), list) else []
        if len(task_ids) != 1:
            raise RuntimeError("确认上下文损坏：缺少任务 ID。")
        task_id = _parse_task_id(str(task_ids[0]))
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
                    *_command_panel(prefix, _card_commands("tasks")),
                ],
                template="green",
            )
        )
    if action == "delete_task_batch":
        task_ids = pending.get("task_ids") if isinstance(pending.get("task_ids"), list) else []
        normalized = [_parse_task_id(str(item)) for item in task_ids]
        return _reply_card(build_batch_task_action_card("delete", batch_task_action(normalized, "delete"), prefix=prefix))
    if action == "delete_project":
        project_name = str(pending.get("project_name") or "").strip()
        if not project_name:
            raise RuntimeError("确认上下文损坏：缺少项目名。")
        return _reply_card(build_project_deleted_card(delete_project_action(project_name), prefix=prefix))
    raise RuntimeError(f"未支持的确认动作：{action or '-'}。")


def _confirm_pending_action(token: str, *, chat_id: str = "", prefix: str = "") -> dict[str, Any]:
    pending = _load_pending_confirm(chat_id, allow_expired=True)
    normalized = str(token or "").strip().upper()
    if not pending:
        return _reply_card(build_confirm_invalid_card(prefix=prefix))
    if str(pending.get("token") or "").strip().upper() != normalized:
        return _reply_card(build_confirm_invalid_card(prefix=prefix, reason="确认口令不匹配，请使用确认卡片里的命令。"))
    expires_at = _parse_iso_datetime(pending.get("expires_at"))
    if expires_at is None or expires_at <= datetime.now():
        _clear_pending_confirm(chat_id)
        return _reply_card(build_confirm_invalid_card(prefix=prefix))
    _clear_pending_confirm(chat_id)
    return _execute_pending_confirm(pending, prefix=prefix)


def _cancel_pending_confirm(token: str, *, chat_id: str = "", prefix: str = "") -> dict[str, Any]:
    pending = _load_pending_confirm(chat_id, allow_expired=True)
    normalized = str(token or "").strip().upper()
    if not pending:
        return _reply_card(build_confirm_invalid_card(prefix=prefix))
    if str(pending.get("token") or "").strip().upper() != normalized:
        return _reply_card(build_confirm_invalid_card(prefix=prefix, reason="确认口令不匹配，无法取消当前确认。"))
    _clear_pending_confirm(chat_id)
    return _reply_card(build_confirm_cancelled_card(normalized, prefix=prefix))


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
            *_section_note("常用入口", "先进入项目，再查看任务、需求或服务状态。"),
            *_command_action_blocks(
                prefix,
                [
                    ("global", "全局状态"),
                    ("projects", "项目清单"),
                    ("overview", "项目总览"),
                    ("tasks", "任务面板"),
                    ("requirements", "需求会话"),
                    ("services", "服务状态"),
                ],
            ),
            *_section_note("任务操作", "请先进入任务面板；具体任务行会提供详情、日志、停止、重试和删除按钮。"),
            *_command_action_blocks(
                prefix,
                [
                    ("tasks status=backlog", "待执行"),
                    ("tasks status=in_progress", "执行中"),
                    ("tasks status=failed", "失败任务"),
                    ("tasks status=done", "已完成"),
                ],
            ),
            *_section_note("服务控制", "轮询负责执行任务，巡检负责发现可改进项。"),
            *_command_action_blocks(
                prefix,
                [
                    ("daemon status", "轮询状态"),
                    ("daemon start", "启动轮询"),
                    ("daemon stop", "停止轮询"),
                    ("inspect status", "巡检状态"),
                    ("inspect start", "启动巡检"),
                    ("inspect stop", "停止巡检"),
                ],
            ),
        ]
    )
    return _card("CodePilot 飞书命令", blocks, template="indigo", subtitle="常用操作已改为卡片按钮，可直接点击执行。")


def build_choice_card(message: str, options: list[dict[str, Any]], *, prefix: str = "") -> dict[str, Any]:
    blocks: list[str | dict[str, Any]] = [
        _section("请确认操作"),
        _plain_block(str(message or "").strip()),
        *_choice_action_blocks(prefix, options),
        _note("点击候选按钮继续；发送新的完整命令会覆盖这次候选。"),
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
                    f"**服务**\n轮询 {_service_mark(task_status)}\n巡检 {_service_mark(inspect_status)}",
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
                f"**飞书长连接**\n{_service_mark(feishu_status)}\nPID `{feishu_status.get('pid') or 0}`",
                f"**Web UI**\n{_service_mark(webui_status)}\nPID `{webui_status.get('pid') or 0}`",
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


def build_project_info_card(project_name: str, *, prefix: str = "") -> dict[str, Any]:
    blocks = _project_status_blocks(project_name)
    blocks.extend(_command_panel(prefix, _card_commands("project_manage", project=project_name)))
    return _card(
        f"CodePilot 项目信息 · {project_name}",
        blocks,
        template="wathet",
        subtitle="项目路径、任务统计和服务状态。",
    )


def build_project_registered_card(result: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    project = result.get("project") if isinstance(result.get("project"), dict) else {}
    project_name = str(project.get("name") or "").strip()
    action = "已注册" if result.get("created") else "已更新"
    config_file = str(result.get("config_file") or project.get("config_file") or "").strip() or "-"
    blocks: list[str | dict[str, Any]] = [
        _field_block(
            [
                _field(f"**项目**\n`{project_name or '-'}`"),
                _field(f"**结果**\n`{action}`"),
                _field(f"**配置文件**\n`{config_file}`", is_short=False),
            ]
        ),
        _hr(),
        *_project_status_blocks(project_name),
    ]
    blocks.extend(_command_panel(prefix, _card_commands("project_manage", project=project_name)))
    return _card(
        f"项目{action} · {project_name}",
        blocks,
        template="green",
        subtitle="项目登记已写入本机数据库；不会操作远程目录。",
    )


def build_overview_card(project_name: str, *, prefix: str = "") -> dict[str, Any]:
    blocks = _project_status_blocks(project_name)
    blocks.extend(_command_panel(prefix, _card_commands("project", project=project_name)))
    return _card(f"CodePilot 项目总览 · {project_name}", blocks, template="wathet", subtitle="项目路径、任务统计和服务状态。")


def build_tasks_card(project_name: str, *, prefix: str = "", status_filter: str = "all", page: int = 1) -> dict[str, Any]:
    project = db.get_project(project_name)
    if project is None:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")

    normalized_status = _normalize_task_status_filter(status_filter)
    page_size = _TASK_PANEL_PAGE_SIZE
    requested_page = max(1, int(page or 1))
    stats = db.get_task_stats(project_name)
    visible_tasks = sort_tasks_for_display(db.list_tasks(project=project_name))
    if normalized_status == "all":
        raw_tasks = [task for task in visible_tasks if str(task.get("status") or "") != "archived"]
    else:
        raw_tasks = [task for task in visible_tasks if str(task.get("status") or "") == normalized_status]
    total_filtered = len(raw_tasks)
    total_pages = max(1, math.ceil(total_filtered / page_size)) if total_filtered else 1
    current_page = min(requested_page, total_pages)
    page_start = (current_page - 1) * page_size
    tasks = [_task_payload(task) for task in raw_tasks[page_start:page_start + page_size]]
    blocks: list[str | dict[str, Any]] = [
        *_section_note("任务概览", "先看异常和执行中任务；每条任务下方提供直接操作按钮。"),
        *_column_panels(
            [
                f"**执行中**\n<font color=\"green\">{int(stats.get('in_progress', 0))}</font>",
                f"**失败**\n<font color=\"red\">{int(stats.get('failed', 0))}</font>",
                f"**待执行**\n<font color=\"grey\">{int(stats.get('backlog', 0))}</font>",
                f"**完成**\n<font color=\"green\">{int(stats.get('done', 0))}</font>",
            ],
            per_row=4,
            background="default",
        ),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**当前筛选**\n`{_task_filter_label(normalized_status)}`",
                f"**命中 / 总数**\n`{total_filtered}/{_task_count(project_name)}`",
                f"**当前页**\n`{current_page}/{total_pages}`",
            ],
            per_row=4,
        )
    ]
    if tasks:
        blocks.append(_hr())
        blocks.extend(
            _section_note(
                "重点任务",
                f"第 {current_page} 页，每页 {page_size} 条；优先展示执行中、失败和高优先级任务。",
            )
        )
        for task in tasks:
            blocks.extend(_task_row_blocks(task, prefix=prefix))
            blocks.append(_hr())
        if blocks and blocks[-1].get("tag") == "hr":
            blocks.pop()
    else:
        blocks.extend([_section("重点任务"), _plain_block("当前筛选下没有可展示的任务。")])
    blocks.extend(
        _command_panel(
            prefix,
            _task_list_commands(
                tasks,
                project_name=project_name,
                status_filter=normalized_status,
                page=current_page,
                total_pages=total_pages,
            ),
        )
    )
    return _card(
        f"CodePilot 任务面板 · {project_name}",
        blocks,
        template="turquoise",
        subtitle="任务列表按当前优先级和状态排序展示，支持状态筛选和分页。",
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
                f"**状态**\n{_status_mark(task['status'])}",
                f"**优先级**\n{_priority_badge(task['priority'])}",
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
    blocks: list[str | dict[str, Any]] = [
        *_section_note("日志摘要", "日志详情已通过富文本消息发送，卡片保留操作入口。"),
        *_column_panels(
            [
                f"**项目**\n`{detail.get('project') or '-'}`",
                f"**状态**\n{_status_mark(detail.get('status') or '')}",
                f"**当前阶段**\n`{_phase_label(detail.get('phase') or '-')}`",
            ]
        ),
        _section("标题"),
        _plain_block(detail.get("title") or ""),
    ]
    blocks.extend([_section("富文本详情"), _plain_block("日志正文会作为下一条富文本消息发送。")])
    blocks.extend(_command_panel(prefix, _card_commands("task", task_id=task_id)))
    return _card(f"任务日志 · #{task_id}", blocks, template="grey", subtitle="卡片用于操作，日志正文使用富文本消息展示。")


def _build_task_log_reply(task_id: int, *, prefix: str = "") -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    detail = task_detail_payload(task_id)
    text = str(detail.get("log_text") or _compose_log_text(task) or "").strip()
    return _reply_multi(
        [
            _reply_card(build_task_log_card(task_id, prefix=prefix)),
            _reply_post(f"任务日志 · #{task_id}", text or "当前没有可展示的日志。"),
        ]
    )


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
                f"**服务状态**\n{_service_mark(status)}",
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
    return _card(f"{title} · {project_name}", blocks, template="purple", subtitle="服务控制结果和快捷操作按钮。")


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
                f"**状态**\n{_status_mark(status)}",
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
                run_async=True,
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
            run_async=True,
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


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _external_notifications_allowed() -> bool:
    if _truthy_env("CODEPILOT_SUPPRESS_EXTERNAL_NOTIFICATIONS"):
        return False
    if "PYTEST_CURRENT_TEST" in os.environ and not _truthy_env("CODEPILOT_ALLOW_TEST_NOTIFICATIONS"):
        return False
    return True


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
    return _normalize_command_alias(content)


_EXACT_COMMAND_ALIASES = {
    "帮助": "help",
    "使用帮助": "help",
    "命令": "help",
    "命令帮助": "help",
    "项目": "projects",
    "项目列表": "projects",
    "项目清单": "projects",
    "所有项目": "projects",
    "全部项目": "projects",
    "有哪些项目": "projects",
    "看项目": "projects",
    "查看项目": "projects",
    "任务": "tasks",
    "任务列表": "tasks",
    "任务清单": "tasks",
    "任务面板": "tasks",
    "所有任务": "tasks",
    "全部任务": "tasks",
    "看任务": "tasks",
    "查看任务": "tasks",
    "查任务": "tasks",
    "需求列表": "requirements",
    "会话列表": "requirements",
    "需求会话": "requirements",
    "会话": "requirements",
    "服务": "services",
    "服务列表": "services",
    "服务状态": "services",
    "服务情况": "services",
    "状态": "overview",
    "项目状态": "overview",
    "项目概览": "overview",
    "项目总览": "overview",
    "总览": "overview",
    "全局": "global",
    "全局状态": "global",
    "整体状态": "global",
}

_TASK_ACTION_ALIASES = (
    ("logs", ("任务日志", "查看日志", "日志")),
    ("detail", ("任务详情", "查看任务", "任务信息", "任务内容", "详情")),
    ("retry", ("重试任务", "重新执行任务", "再跑任务")),
    ("stop", ("停止任务", "终止任务", "停掉任务")),
    ("cancel", ("取消任务",)),
    ("archive", ("归档任务",)),
    ("delete", ("删除任务", "删掉任务", "移除任务")),
)


def _compact_alias_text(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？:：；;、]+", "", str(text or "").strip())


def _normalize_command_alias(content: str) -> str:
    raw = str(content or "").strip()
    if not raw:
        return raw
    compact = _compact_alias_text(raw)
    alias = _EXACT_COMMAND_ALIASES.get(compact)
    if alias:
        return alias

    for command, aliases in _TASK_ACTION_ALIASES:
        for label in aliases:
            match = re.fullmatch(rf"{re.escape(label)}\s*#?(\d+)", raw)
            if match:
                return f"{command} {match.group(1)}"
            match = re.fullmatch(rf"#?(\d+)\s*{re.escape(label)}", raw)
            if match:
                return f"{command} {match.group(1)}"
    return raw


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


_MAX_BATCH_RESULT_LINES = 8


def _positive_batch_task_id(value: Any) -> int:
    try:
        task_id = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return task_id if task_id > 0 else 0


def _batch_success_task_id(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    task = item.get("task") if isinstance(item.get("task"), dict) else {}
    return _positive_batch_task_id(
        (task or {}).get("id")
        or item.get("deleted_task_id")
        or item.get("task_id")
    )


def _first_batch_success_task_id(succeeded: list[Any]) -> int:
    for item in succeeded:
        task_id = _batch_success_task_id(item)
        if task_id:
            return task_id
    return 0


def _batch_action_summary_block(action_label: str, result: dict[str, Any]) -> dict[str, Any]:
    return _field_block(
        [
            _field(f"**操作**\n`批量{action_label}`"),
            _field(f"**总计**\n`{int(result.get('total') or 0)}`"),
            _field(f"**成功**\n`{int(result.get('success_count') or 0)}`"),
            _field(f"**失败**\n`{int(result.get('failed_count') or 0)}`"),
        ]
    )


def _batch_success_blocks(succeeded: list[Any]) -> list[dict[str, Any]]:
    success_lines: list[str] = []
    for item in succeeded[:_MAX_BATCH_RESULT_LINES]:
        if not isinstance(item, dict):
            continue
        task_id = _batch_success_task_id(item)
        if task_id <= 0:
            continue
        success_lines.append(f"- `#{task_id}` {str(item.get('message') or '').strip()}")
    if not success_lines:
        return []
    return [*_section_note("成功任务"), _md_block("\n".join(success_lines))]


def _batch_failed_blocks(failed: list[Any]) -> list[dict[str, Any]]:
    failed_lines: list[str] = []
    for item in failed[:_MAX_BATCH_RESULT_LINES]:
        if not isinstance(item, dict):
            continue
        task_id = _positive_batch_task_id(item.get("task_id"))
        error = str(item.get("error") or "").strip()
        if task_id <= 0:
            continue
        failed_lines.append(f"- `#{task_id}` {error}")
    if not failed_lines:
        return []
    return [*_section_note("失败任务"), _md_block("\n".join(failed_lines))]


def _batch_action_commands(action_key: str, first_task_id: int) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = [("tasks", "任务面板")]
    if first_task_id and action_key != "delete":
        commands.insert(0, (f"detail {first_task_id}", "查看首个成功任务"))
        commands.insert(1, (f"logs {first_task_id}", "查看首个任务日志"))
    return commands


def _batch_action_template(result: dict[str, Any]) -> str:
    return "green" if int(result.get("failed_count") or 0) == 0 else "orange"


def build_batch_task_action_card(action: str, result: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    action_label = _batch_action_label(action_key)
    succeeded = result.get("succeeded") or []
    failed = result.get("failed") or []
    blocks: list[str | dict[str, Any]] = [
        _batch_action_summary_block(action_label, result),
        _note(str(result.get("message") or "").strip()),
    ]
    blocks.extend(_batch_success_blocks(succeeded))
    blocks.extend(_batch_failed_blocks(failed))
    blocks.extend(
        _command_panel(
            prefix,
            _batch_action_commands(action_key, _first_batch_success_task_id(succeeded)),
        )
    )
    return _card(
        f"批量{action_label}完成",
        blocks,
        template=_batch_action_template(result),
        subtitle="飞书已执行批量任务操作，并返回逐项结果。",
    )


def _handle_pending_command_choice(command_text: str, context: _CommandContext) -> dict[str, Any] | None:
    cfg = context.cfg
    chat_id = context.chat_id
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
            config_path=context.config_path,
            chat_id=chat_id,
            _normalized_command_text=selected_command,
            _allow_nl=False,
        )
    if pending_options and str(command_text).isdigit():
        return _reply_card(build_choice_card("可选项超出范围，请重新选择：", pending_options, prefix=cfg.command_prefix))
    if pending_options:
        _clear_pending_action_options(chat_id)
        _clear_pending_goal_text(chat_id)
    return None


def _handle_control_command(verb: str, parts: list[str], context: _CommandContext) -> dict[str, Any] | None:
    cfg = context.cfg
    chat_id = context.chat_id
    if verb == "confirm":
        if len(parts) < 2:
            raise RuntimeError("请提供确认口令，例如 `confirm ABC123`。")
        return _confirm_pending_action(parts[1], chat_id=chat_id, prefix=cfg.command_prefix)
    if verb == "cancel" and len(parts) > 1 and parts[1].lower() == "confirm":
        if len(parts) < 3:
            raise RuntimeError("请提供确认口令，例如 `cancel confirm ABC123`。")
        return _cancel_pending_confirm(parts[2], chat_id=chat_id, prefix=cfg.command_prefix)
    if verb in {"help", "h", "?"}:
        return _reply_card(build_help_card(prefix=cfg.command_prefix))
    if verb in {"global", "summary", "all"}:
        return _reply_card(
            build_global_status_card(prefix=cfg.command_prefix, default_project=_active_project(cfg, chat_id))
        )
    return None


def _handle_project_command(
    verb: str,
    parts: list[str],
    command_text: str,
    context: _CommandContext,
) -> dict[str, Any] | None:
    cfg = context.cfg
    chat_id = context.chat_id
    active_project = _active_project(cfg, chat_id)
    if verb == "project" and len(parts) > 1:
        subcommand = parts[1].lower()
        if subcommand in {"add", "register"}:
            if len(parts) < 4:
                raise RuntimeError("请提供项目名和路径，例如 `project add demo D:\\work\\demo`。")
            project_name = parts[2].strip()
            project_path = command_text.split(None, 3)[3].strip() if len(parts) > 3 else ""
            try:
                result = create_project_action(project_path, name=project_name)
            except Exception as exc:
                return _reply_card(build_project_command_error_card("项目注册失败", str(exc), prefix=cfg.command_prefix))
            if chat_id:
                _save_chat_project(chat_id, str(result.get("project", {}).get("name") or project_name).strip())
            return _reply_card(build_project_registered_card(result, prefix=cfg.command_prefix))
        if subcommand in {"info", "show"}:
            try:
                project_name = _resolve_project(parts[2] if len(parts) > 2 else "", default_project=active_project)
                return _reply_card(build_project_info_card(project_name, prefix=cfg.command_prefix))
            except Exception as exc:
                return _reply_card(build_project_command_error_card("项目信息不可用", str(exc), prefix=cfg.command_prefix))
        if subcommand in {"delete", "rm", "remove"}:
            if len(parts) < 3:
                raise RuntimeError("请提供项目名，例如 `project delete demo`。")
            try:
                project_name = _resolve_project(parts[2], default_project=active_project)
                pending = _pending_project_delete_confirm(project_name, command_text=command_text, chat_id=chat_id)
            except Exception as exc:
                return _reply_card(build_project_command_error_card("项目删除不可用", str(exc), prefix=cfg.command_prefix))
            _save_pending_confirm(chat_id, pending)
            return _reply_card(build_pending_confirm_card(pending, prefix=cfg.command_prefix))
    if verb not in {"use", "project"}:
        return None
    if len(parts) < 2:
        if active_project:
            return _reply_card(build_overview_card(active_project, prefix=cfg.command_prefix))
        return _reply_card(build_projects_card(prefix=cfg.command_prefix, default_project=active_project))
    project_name = _resolve_project(parts[1], default_project=active_project)
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


def _handle_project_view_command(
    verb: str,
    parts: list[str],
    context: _CommandContext,
) -> dict[str, Any] | None:
    cfg = context.cfg
    chat_id = context.chat_id
    active_project = _active_project(cfg, chat_id)
    if verb in {"overview", "ov"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=active_project)
        return _reply_card(build_overview_card(project_name, prefix=cfg.command_prefix))
    if verb in {"projects", "ls"}:
        return _reply_card(build_projects_card(prefix=cfg.command_prefix, default_project=active_project))
    if verb in {"tasks", "panel"}:
        project_name, status_filter, page = _parse_tasks_command_args(parts[1:], default_project=active_project)
        return _reply_card(
            build_tasks_card(
                project_name,
                prefix=cfg.command_prefix,
                status_filter=status_filter,
                page=page,
            )
        )
    if verb in {"requirements", "sessions", "jobs"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=active_project)
        return _reply_card(build_sessions_card(project_name, prefix=cfg.command_prefix))
    return None


def _handle_requirement_command(
    verb: str,
    parts: list[str],
    command_text: str,
    context: _CommandContext,
) -> dict[str, Any] | None:
    cfg = context.cfg
    chat_id = context.chat_id
    active_project = _active_project(cfg, chat_id)
    if verb == "req":
        if len(parts) > 1 and parts[1].lower() == "new":
            requirement_text = command_text.split(None, 2)[2].strip() if len(parts) > 2 else ""
        else:
            requirement_text = command_text[len(parts[0]):].strip()
        if not requirement_text:
            raise RuntimeError("请在命令后写需求内容，例如 `req new 优化飞书任务卡片`。")
        project_name = _resolve_project("", default_project=active_project)
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
        project_name = _resolve_project("", default_project=active_project)
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
        project_name = _resolve_project("", default_project=active_project)
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
        project_name = _resolve_project("", default_project=active_project)
        if not _load_pending_requirement(chat_id, project_name):
            raise RuntimeError("当前没有等待补充的需求。请先发送 `需求 <内容>`。")
        return _submit_requirement_from_feishu(
            project_name,
            answer_text,
            cfg=cfg,
            chat_id=chat_id,
            continue_pending=True,
        )
    if verb not in {"session", "job"}:
        return None
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


def _handle_task_command(
    verb: str,
    parts: list[str],
    command_text: str,
    context: _CommandContext,
) -> dict[str, Any] | None:
    cfg = context.cfg
    chat_id = context.chat_id
    if verb in {"detail", "task", "show"}:
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `detail 123`。")
        return _reply_card(build_task_card(_parse_task_id(parts[1]), prefix=cfg.command_prefix))
    if verb in {"logs", "log"}:
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `logs 123`。")
        return _build_task_log_reply(_parse_task_id(parts[1]), prefix=cfg.command_prefix)
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
        pending = _pending_delete_confirm(task_ids, command_text=command_text, chat_id=chat_id)
        _save_pending_confirm(chat_id, pending)
        return _reply_card(build_pending_confirm_card(pending, prefix=cfg.command_prefix))
    if verb == "retry":
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `retry 123`。")
        task_id = _parse_task_id(parts[1])
        retry_task_action(task_id)
        return _reply_card(build_task_card(task_id, prefix=cfg.command_prefix, title_prefix="任务已重试"))
    return None


def _handle_service_command(verb: str, parts: list[str], context: _CommandContext) -> dict[str, Any] | None:
    cfg = context.cfg
    chat_id = context.chat_id
    active_project = _active_project(cfg, chat_id)
    if verb in {"run", "start"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=active_project)
        result = project_service_action(project_name, "tasks", "start")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title="任务执行服务已启动"))
    if verb in {"daemon", "worker"}:
        action = parts[1].lower() if len(parts) > 1 else "status"
        project_name = _resolve_project(parts[2] if len(parts) > 2 else "", default_project=active_project)
        result = project_service_action(project_name, "tasks", action)
        title = {
            "start": "任务轮询已启动",
            "stop": "任务轮询停止请求",
            "status": "任务轮询状态",
        }.get(action, "任务轮询状态")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title=title))
    if verb == "inspect":
        action = parts[1].lower() if len(parts) > 1 else "status"
        project_name = _resolve_project(parts[2] if len(parts) > 2 else "", default_project=active_project)
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
                build_global_status_card(prefix=cfg.command_prefix, default_project=active_project)
            )
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=active_project)
        return _reply_card(build_services_card(project_name, prefix=cfg.command_prefix))
    if verb == "status":
        if len(parts) > 1 and parts[1].lower() in {"all", "global"}:
            return _reply_card(
                build_global_status_card(prefix=cfg.command_prefix, default_project=active_project)
            )
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=active_project)
        result = project_service_action(project_name, "tasks", "status")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title="任务执行服务状态"))
    return None


def _handle_natural_language_command(
    command_text: str,
    context: _CommandContext,
    *,
    allow_nl: bool,
) -> dict[str, Any] | None:
    if not allow_nl:
        return None
    cfg = context.cfg
    chat_id = context.chat_id
    active_project = _active_project(cfg, chat_id)
    resolved = resolve_natural_language_command(command_text, active_project=active_project)
    if resolved.get("status") == "options":
        options = list(resolved.get("options") or [])
        _save_pending_action_options(chat_id, options)
        return _reply_card(build_choice_card(str(resolved.get("message") or "请确认操作"), options, prefix=cfg.command_prefix))
    if resolved.get("status") == "match":
        _clear_pending_action_options(chat_id)
        command = str(resolved.get("command") or "")
        return handle_command_text(
            command,
            config_path=context.config_path,
            chat_id=chat_id,
            _normalized_command_text=command,
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
                {
                    "command": f"use {str(project.get('name') or '').strip()}",
                    "label": f"在项目 {str(project.get('name') or '').strip()} 中继续",
                }
                for project in projects[:4]
                if str(project.get("name") or "").strip()
            ]
            if options:
                _save_pending_action_options(chat_id, options)
                _save_pending_goal_text(chat_id, command_text)
                return _reply_card(
                    build_choice_card(
                        "这条消息会按 chat 处理。先确认你要在哪个项目里继续：",
                        options,
                        prefix=cfg.command_prefix,
                    )
                )
    if fallback_project:
        return _submit_goal_from_feishu(
            fallback_project,
            command_text,
            chat_id=chat_id,
            prefix=cfg.command_prefix,
        )
    return None


def _dispatch_command_text(
    command_text: str,
    parts: list[str],
    context: _CommandContext,
    *,
    allow_nl: bool,
) -> dict[str, Any] | None:
    verb = parts[0].lower()
    reply = _handle_control_command(verb, parts, context)
    if reply is not None:
        return reply
    for handler, needs_command_text in (
        (_handle_project_command, True),
        (_handle_project_view_command, False),
        (_handle_requirement_command, True),
        (_handle_task_command, True),
        (_handle_service_command, False),
    ):
        reply = handler(verb, parts, command_text, context) if needs_command_text else handler(verb, parts, context)
        if reply is not None:
            return reply
    return _handle_natural_language_command(command_text, context, allow_nl=allow_nl)


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
    context = _CommandContext(cfg=cfg, config_path=config_path, chat_id=chat_id)
    command_text = _normalized_command_text
    if command_text is None:
        command_text = _normalize_command_text(text, cfg.command_prefix)
    if command_text is None:
        return {"type": "ignore"}
    pending_reply = _handle_pending_command_choice(command_text, context)
    if pending_reply is not None:
        return pending_reply

    parts = command_text.split()
    if not parts:
        return _reply_card(build_help_card(prefix=cfg.command_prefix))
    reply = _dispatch_command_text(command_text, parts, context, allow_nl=_allow_nl)
    if reply is not None:
        return reply
    return _reply_card(build_help_card(prefix=cfg.command_prefix, error=f"`{command_text}`"))


def _card_action_event(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    return event if isinstance(event, dict) else {}


def _card_action_chat_id(event: dict[str, Any]) -> str:
    context = event.get("context") if isinstance(event.get("context"), dict) else {}
    return str(
        event.get("chat_id")
        or event.get("open_chat_id")
        or context.get("open_chat_id")
        or context.get("chat_id")
        or ""
    ).strip()


def _card_action_command(event: dict[str, Any]) -> str:
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    value = action.get("value") if isinstance(action.get("value"), dict) else {}
    for key in ("command", "cmd", "text"):
        command = str(value.get(key) or "").strip()
        if command:
            return command
    return ""


def handle_card_action_payload(payload: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    """Handle a Feishu card button callback using the same command router as text messages."""
    db.init_db()
    event = _card_action_event(payload)
    command = _card_action_command(event)
    if not command:
        cfg = load_feishu_bot_config(config_path)
        return _reply_card(build_help_card(prefix=cfg.command_prefix, error="卡片按钮缺少 command"))
    return handle_command_text(command, config_path=config_path, chat_id=_card_action_chat_id(event))


def handle_event_payload(payload: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    if str(payload.get("event_type") or payload.get("type") or "").strip() == "card.action.trigger":
        return handle_card_action_payload(payload, config_path=config_path)

    chat_id = str(payload.get("chat_id") or "")
    dedupe_key = _inbound_dedupe_key(payload)
    claimed = True
    if dedupe_key:
        claimed = db.claim_service_state(
            _INBOUND_DEDUPE_SERVICE,
            dedupe_key,
            pid=0,
            status="processing",
            log_path="",
            meta={
                "chat_id": chat_id,
                "event_id": str(payload.get("event_id") or "").strip(),
                "message_id": str(payload.get("message_id") or "").strip(),
                "text": str(payload.get("text") or "")[:200],
                "received_at": _now_iso(),
            },
        )
    if not claimed:
        return {"type": "ignore"}

    try:
        _touch_chat_seen(chat_id)
        reply = handle_command_text(
            str(payload.get("text") or ""),
            config_path=config_path,
            chat_id=chat_id,
        )
        if dedupe_key:
            db.upsert_service_state(
                _INBOUND_DEDUPE_SERVICE,
                dedupe_key,
                pid=0,
                status="done",
                log_path="",
                meta={
                    "chat_id": chat_id,
                    "event_id": str(payload.get("event_id") or "").strip(),
                    "message_id": str(payload.get("message_id") or "").strip(),
                    "text": str(payload.get("text") or "")[:200],
                    "handled_at": _now_iso(),
                },
            )
        return reply
    except Exception as exc:
        if dedupe_key:
            db.upsert_service_state(
                _INBOUND_DEDUPE_SERVICE,
                dedupe_key,
                pid=0,
                status="failed",
                log_path="",
                meta={
                    "chat_id": chat_id,
                    "event_id": str(payload.get("event_id") or "").strip(),
                    "message_id": str(payload.get("message_id") or "").strip(),
                    "text": str(payload.get("text") or "")[:200],
                    "failed_at": _now_iso(),
                    "error": str(exc)[:300],
                },
            )
        cfg = load_feishu_bot_config(config_path)
        return _reply_card(
            _card(
                "CodePilot 命令执行失败",
                [str(exc)],
                template="red",
                note=_help_note(cfg.command_prefix),
            )
        )
