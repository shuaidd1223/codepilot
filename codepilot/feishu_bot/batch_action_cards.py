"""Batch task action card builders for Feishu bot."""

from __future__ import annotations

from typing import Any

from codepilot.feishu_bot.constants import _MAX_BATCH_RESULT_LINES
from codepilot.feishu_cards import (
    _card,
    _command_panel,
    _field,
    _field_block,
    _md_block,
    _note,
    _section_note,
)


def _batch_action_label(action: str) -> str:
    return {
        "cancel": "取消",
        "archive": "归档",
        "delete": "删除",
    }.get(str(action or "").strip().lower(), str(action or "").strip())


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
