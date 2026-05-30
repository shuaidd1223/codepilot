"""Feishu bot command routing and handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.feishu_bot.card_builders import (
    _build_task_log_reply,
    _cancel_pending_confirm,
    _confirm_pending_action,
    _submit_opencode_from_feishu,
    build_batch_task_action_card,
    build_choice_card,
    build_global_status_card,
    build_help_card,
    build_overview_card,
    build_project_command_error_card,
    build_project_info_card,
    build_project_registered_card,
    build_projects_card,
    build_pending_confirm_card,
    build_service_card,
    build_services_card,
    build_session_card,
    build_sessions_card,
    build_task_card,
    build_tasks_card,
)
from codepilot.feishu_bot.constants import _INBOUND_DEDUPE_SERVICE
from codepilot.feishu_bot.helpers import (
    _active_project,
    _card_commands,
    _clear_pending_action_options,
    _clear_pending_goal_text,
    _CommandContext,
    _load_pending_action_options,
    _load_pending_goal_text,
    _now_iso,
    _parse_tasks_command_args,
    _pending_delete_confirm,
    _pending_project_delete_confirm,
    _resolve_project,
    _save_chat_project,
    _save_pending_action_options,
    _save_pending_confirm,
    _save_pending_goal_text,
    _touch_chat_seen,
)
from codepilot.feishu_cards import (
    _card,
    _command_panel,
    _field,
    _field_block,
    _help_note,
    _plain_block,
    _reply_card,
    _section,
)
from codepilot.feishu_commands import (
    normalize_command_text as _normalize_command_text,
    parse_task_id as _parse_task_id,
    parse_task_ids as _parse_task_ids,
)
from codepilot.feishu_config import load_feishu_bot_config
from codepilot.feishu_interactions import (
    card_action_chat_id as _card_action_chat_id,
    card_action_command as _card_action_command,
    card_action_event as _card_action_event,
    inbound_dedupe_key as _inbound_dedupe_key,
)
from codepilot.nl_command_router import pick_command_option
from codepilot.storage import database as db
from codepilot.webapp.action_task_ops import (
    archive_task_action,
    batch_task_action,
    cancel_task_action,
    create_project_action,
    project_service_action,
    stop_task_action,
)
from codepilot.webapp.action_requirements import retry_task_action


# ---------------------------------------------------------------------------
# Pending choice handler
# ---------------------------------------------------------------------------


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
            return _submit_opencode_from_feishu(
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


# ---------------------------------------------------------------------------
# Control command handler
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Project command handler
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Project view command handler
# ---------------------------------------------------------------------------


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
    if verb in {"workflow", "next"}:
        if verb == "workflow" and len(parts) > 1 and parts[1].lower() == "next":
            if len(parts) < 4:
                raise RuntimeError("请提供项目和动作，例如 `workflow next demo create_inspect_tasks`。")
            project_name = _resolve_project(parts[2], default_project=active_project)
            action_id = parts[3]
            from codepilot.commands.workflow import execute_workflow_auto_next_action, execute_workflow_next_action

            if action_id.lower() == "auto":
                result = execute_workflow_auto_next_action(project_name)
                title = "工作流自动推进已执行" if result.get("action") else "工作流暂无自动动作"
            else:
                execute_workflow_next_action(project_name, action_id)
                title = "工作流动作已执行"
            return _reply_card(_build_workflow_card(project_name, prefix=cfg.command_prefix, title=title))
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=active_project)
        return _reply_card(_build_workflow_card(project_name, prefix=cfg.command_prefix))
    return None


def _build_workflow_card(project_name: str, *, prefix: str = "", title: str = "CodePilot 工作流下一步") -> dict[str, Any]:
    from codepilot.commands.inspect_workflow import read_inspect_workflow_context
    from codepilot.commands.workflow import workflow_next_payload

    project = db.get_project(project_name)
    if not project:
        raise RuntimeError(f"项目 '{project_name}' 不存在。")
    context = read_inspect_workflow_context(project) or {}
    quality = context.get("quality_summary") or {}
    report_only = context.get("report_only") or []
    next_payload = workflow_next_payload(project_name)
    actions = next_payload.get("next_actions") or []
    auto_policy = next_payload.get("auto_policy") or {}
    blocks: list[str | dict[str, Any]] = [
        _field_block(
            [
                _field(f"**项目**\n`{project_name}`"),
                _field(
                    "**质量摘要**\n"
                    f"`created={quality.get('created_count', len(context.get('created_preview') or []))} "
                    f"report_only={quality.get('report_only_count', len(report_only))}`"
                ),
            ]
        ),
        _field_block(
            [
                _field(
                    "**自动策略**\n"
                    f"`steps={auto_policy.get('max_steps', 1)} "
                    f"create={str(bool(auto_policy.get('allow_create_inspect_tasks'))).lower()} "
                    f"import={str(bool(auto_policy.get('allow_import_plan_tasks'))).lower()} "
                    f"fail={auto_policy.get('failure_threshold', 1)}`"
                ),
            ]
        ),
    ]
    if report_only:
        blocks.append(_section("仅报告建议"))
        blocks.append(_plain_block("\n".join(
            f"- `{item.get('candidate_id')}` {item.get('title')}" for item in report_only[:5]
        )))
    commands = [(f"workflow next {project_name} auto", "自动推进")]
    commands.extend(
        (f"workflow next {project_name} {action.get('id')}", str(action.get("label") or action.get("id")))
        for action in actions[:6]
        if str(action.get("id") or "").strip()
    )
    blocks.extend(_command_panel(prefix, commands, title="工作流动作"))
    return _card(title, blocks, template="blue")


# ---------------------------------------------------------------------------
# Requirement / session command handler
# ---------------------------------------------------------------------------


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
        return _submit_opencode_from_feishu(project_name, requirement_text, chat_id=chat_id, prefix=cfg.command_prefix)
    if verb == "ask":
        user_text = command_text[len(parts[0]):].strip()
        if not user_text:
            raise RuntimeError("请在命令后写内容，例如 `ask 帮我梳理一下最近需求`。")
        project_name = _resolve_project("", default_project=active_project)
        return _submit_opencode_from_feishu(project_name, user_text, chat_id=chat_id, prefix=cfg.command_prefix)
    if verb in {"需求", "requirement", "plan", "new", "goal"}:
        requirement_text = command_text[len(parts[0]):].strip()
        if not requirement_text:
            raise RuntimeError("请在命令后写需求内容，例如 `需求 优化任务面板状态展示`。")
        project_name = _resolve_project("", default_project=active_project)
        return _submit_opencode_from_feishu(project_name, requirement_text, chat_id=chat_id, prefix=cfg.command_prefix)
    if verb in {"答", "answer", "reply"}:
        answer_text = command_text[len(parts[0]):].strip()
        if not answer_text:
            raise RuntimeError("请在命令后写补充答案，例如 `答 先做飞书控制入口`。")
        project_name = _resolve_project("", default_project=active_project)
        return _submit_opencode_from_feishu(project_name, answer_text, chat_id=chat_id, prefix=cfg.command_prefix)
    if verb not in {"session", "job"}:
        return None
    if verb == "session" and len(parts) > 1 and parts[1].lower() in {"reply", "continue"}:
        if len(parts) < 4:
            raise RuntimeError("请提供会话 ID 和内容，例如 `session reply 12 先做飞书入口`。")
        session_id = _parse_task_id(parts[2])
        reply_text = command_text.split(None, 3)[3].strip() if len(parts) > 3 else ""
        if not reply_text:
            raise RuntimeError("请提供会话回复内容，例如 `session reply 12 先做飞书入口`。")
        return _submit_opencode_from_feishu(
            "",
            reply_text,
            chat_id=chat_id,
            prefix=cfg.command_prefix,
            session_id=session_id,
        )
    if len(parts) < 2:
        raise RuntimeError("请提供会话 ID，例如 `session 12`。")
    return _reply_card(build_session_card(_parse_task_id(parts[1]), prefix=cfg.command_prefix))


# ---------------------------------------------------------------------------
# Task command handler
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Service command handler
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Natural language handler
# ---------------------------------------------------------------------------


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
    if active_project and not db.get_project(active_project):
        active_project = ""
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
        return _submit_opencode_from_feishu(
            fallback_project,
            command_text,
            chat_id=chat_id,
            prefix=cfg.command_prefix,
        )
    return None


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
