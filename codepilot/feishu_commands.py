"""Feishu command text normalization and task-id parsing."""

from __future__ import annotations

import re


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
    "当前项目": "overview",
    "当前项目状态": "overview",
    "当前项目概览": "overview",
    "当前项目总览": "overview",
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


normalize_command_text = _normalize_command_text
normalize_command_alias = _normalize_command_alias
parse_task_id = _parse_task_id
parse_task_ids = _parse_task_ids
