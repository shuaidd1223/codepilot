"""Feishu interactive card primitives and shared render helpers."""

from __future__ import annotations

from typing import Any


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


card = _card
reply_card = _reply_card
reply_post = _reply_post
reply_multi = _reply_multi
