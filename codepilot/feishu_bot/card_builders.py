"""Feishu bot card builder functions."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from codepilot.core.work_item import build_work_item
from codepilot.feishu_bot import notification_cards as _notification_cards
from codepilot import feishu_runtime as _feishu_runtime
from codepilot.feishu_bot.batch_action_cards import build_batch_task_action_card
from codepilot.feishu_bot.constants import _PENDING_CONFIRM_TTL_SECONDS
from codepilot.feishu_bot.helpers import (
    _card_commands,
    _clear_pending_confirm,
    _load_pending_confirm,
    _parse_iso_datetime,
    _project_service_status,
    _project_status_blocks,
    _runtime_service_state,
    _session_count,
    _session_followup_commands,
    _session_related_task_ids,
    _task_count,
    _task_filter_label,
    _task_list_commands,
)
from codepilot.feishu_cards import (
    _TASK_PANEL_PAGE_SIZE,
    _card,
    _choice_action_blocks,
    _column_panels,
    _command_action_blocks,
    _command_panel,
    _count_panel,
    _field,
    _field_block,
    _hr,
    _md_block,
    _note,
    _phase_label,
    _plain_block,
    _priority_badge,
    _reply_card,
    _reply_multi,
    _reply_post,
    _running_label,
    _section,
    _section_note,
    _service_mark,
    _status_mark,
    _task_row_blocks,
)
from codepilot.storage import database as db
from codepilot.webapp.action_task_ops import (
    batch_task_action,
    delete_project_action,
    delete_task_action,
)
from codepilot.webapp.display_sort import sort_tasks_for_display
from codepilot.webapp.task_payloads import _compose_log_text, _task_payload, task_detail_payload


# ---------------------------------------------------------------------------
# Confirmation card builders
# ---------------------------------------------------------------------------


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


def _execute_pending_confirm(pending: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    from codepilot.feishu_commands import parse_task_id as _parse_task_id

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


# ---------------------------------------------------------------------------
# Help / Choice / OpenCode answer card builders
# ---------------------------------------------------------------------------


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


def build_opencode_answer_card(project_name: str, user_text: str, result: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    ok = bool(result.get("ok"))
    blocks: list[str | dict[str, Any]] = [
        _section("OpenCode 回复"),
        *_column_panels(
            [
                f"**项目**\n`{project_name}`",
                f"**会话**\n`#{result.get('codepilot_session_id') or '-'}`",
                f"**OpenCode**\n`{result.get('opencode_session_id') or '-'}`",
                f"**状态**\n`{'完成' if ok else '失败'}`",
            ]
        ),
        _section("你的输入"),
        _plain_block(str(user_text or "").strip()[:700]),
        _section("回复内容"),
        _plain_block(str(result.get("message") or "").strip()[:1600] or "OpenCode 没有返回可展示文本。"),
    ]
    tool_calls = result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else []
    if tool_calls:
        names = [
            str(item.get("name") or "").strip()
            for item in tool_calls
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if names:
            blocks.extend([_section("工具调用"), _plain_block("、".join(names[:8]))])
    blocks.extend(_command_panel(prefix, _card_commands("project", project=project_name)))
    return _card(
        f"OpenCode 会话 · {project_name}",
        blocks,
        template="blue" if ok else "red",
        subtitle="飞书消息已交给 OpenCode + CodePilot MCP 处理。",
    )


def _submit_opencode_from_feishu(
    project_name: str,
    user_text: str,
    *,
    chat_id: str = "",
    prefix: str = "",
    session_id: int | None = None,
) -> dict[str, Any]:
    from codepilot.feishu_bot.helpers import (
        _clear_pending_goal_text,
        _resolve_opencode_db_session,
        _save_active_opencode_session_id,
        _save_chat_project,
    )
    from codepilot.opencode.session import run_opencode_message

    content = str(user_text or "").strip()
    if not content:
        raise RuntimeError("输入不能为空。")
    resolved_session_id, canonical_project = _resolve_opencode_db_session(
        project_name,
        content,
        chat_id=chat_id,
        session_id=session_id,
    )
    if chat_id and canonical_project:
        _save_chat_project(chat_id, canonical_project)
        _save_active_opencode_session_id(chat_id, resolved_session_id)
    db.create_session_message(
        resolved_session_id,
        "user",
        content,
        metadata={
            "work_item": build_work_item(
                source="feishu",
                requester=chat_id or "feishu",
                raw_text=content,
                callback={"type": "feishu_chat", "chat_id": chat_id},
            ),
        },
    )

    result = run_opencode_message(
        canonical_project,
        content,
        source="feishu",
        external_session_id=str(resolved_session_id),
    )
    result = dict(result or {})
    result["codepilot_session_id"] = resolved_session_id
    reply = str(result.get("message") or "").strip()
    db.create_session_message(
        resolved_session_id,
        "assistant",
        reply or "OpenCode 没有返回可展示文本。",
        intent="opencode" if result.get("ok") else "error",
        metadata={
            "opencode_session_id": result.get("opencode_session_id") or "",
            "tool_calls": result.get("tool_calls") or [],
        },
    )
    _clear_pending_goal_text(chat_id)
    return _reply_card(build_opencode_answer_card(canonical_project, content, result, prefix=prefix))


# ---------------------------------------------------------------------------
# Project / Global / Overview card builders
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task / Session card builders
# ---------------------------------------------------------------------------


def build_tasks_card(project_name: str, *, prefix: str = "", status_filter: str = "all", page: int = 1) -> dict[str, Any]:
    project = db.get_project(project_name)
    if project is None:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")

    from codepilot.feishu_bot.helpers import _normalize_task_status_filter

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
            _section("会话记录"),
            _plain_block("当前还没有会话记录。可先在 Web UI、CLI 或飞书中发送一条消息。"),
        ]
        blocks.extend(_command_panel(prefix, _card_commands("sessions")))
        return _card(
            f"CodePilot 会话记录 · {project_name}",
            blocks,
            template="orange",
            subtitle="会话记录用于查看 Web UI 和 OpenCode 上下文。",
        )
    blocks: list[str | dict[str, Any]] = [
        *_section_note("会话记录", "最近会话按更新时间展示。"),
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
        f"CodePilot 会话记录 · {project_name}",
        blocks,
        template="carmine",
        subtitle="查看 Web UI 和 OpenCode 会话消息。",
    )


def build_session_card(session_id: int, *, prefix: str = "") -> dict[str, Any]:
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    messages = db.list_session_messages(session_id)
    related_task_ids = _session_related_task_ids(messages)
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
    if messages:
        lines: list[str] = []
        for message in messages[-6:]:
            role = str(message.get("role") or "-")
            content = " ".join(str(message.get("content") or "").split())
            message_task_ids = _get_message_task_ids(message)
            extra = f" / tasks {' '.join(f'#{task_id}' for task_id in message_task_ids)}" if message_task_ids else ""
            lines.append(f"- **{role}**：{content[:100]}{extra}")
        blocks.extend([_section("最近消息"), _md_block("\n".join(lines))])
    if related_task_ids:
        blocks.extend([_section("相关任务"), _plain_block("、".join(f"#{task_id}" for task_id in related_task_ids))])
    blocks.extend(_command_panel(prefix, _session_followup_commands(session_id, related_task_ids=related_task_ids)))
    return _card(f"OpenCode 会话详情 · #{session_id}", blocks, template="violet", subtitle="会话消息来自 OpenCode + CodePilot MCP。")


def _get_message_task_ids(message: dict[str, Any]) -> list[int]:
    from codepilot.feishu_bot.helpers import _message_task_ids

    return _message_task_ids(message)


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
    return _card(f"任务日志 · #{task_id}", blocks, template="grey", subtitle="卡片用于操作，日志正文使用富文本消息发送。")


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


# ---------------------------------------------------------------------------
# Notification / event card builders
# ---------------------------------------------------------------------------


_feishu_notify_script = _feishu_runtime.notify_script


def _send_bot_card(card: dict[str, Any], *, project_name: str = "", chat_ids: list[str] | None = None) -> bool:
    return _notification_cards._send_bot_card(card, project_name=project_name, chat_ids=chat_ids)


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
    return _notification_cards.build_task_event_card(
        project_name=project_name,
        task_id=task_id,
        task_title=task_title,
        event=event,
        phase=phase,
        level=level,
        message=message,
        status=status,
        summary=summary,
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
) -> bool:
    return _notification_cards.notify_feishu_task_event(
        project_name=project_name,
        project_path=project_path,
        task_id=task_id,
        task_title=task_title,
        event=event,
        phase=phase,
        level=level,
        message=message,
        status=status,
        summary=summary,
        chat_ids=chat_ids,
        _send_card=_send_bot_card,
    )
