"""Natural-language command dispatch helpers for interactive chat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import click
from click.testing import CliRunner as _InlineRunner

from codepilot.nl_command_router import (
    _normalize_text as _normalize_nl_text,
    format_numbered_options,
    pick_command_option,
    resolve_natural_language_command,
)
from codepilot.storage import database as db
from codepilot.webapp.action_task_ops import project_service_action


@dataclass(frozen=True)
class _ParsedChatCommand:
    """Normalized local command resolved from natural-language routing."""

    raw: str
    parts: list[str]
    verb: str


def _active_chat_project_name(runtime: Any) -> str:
    if runtime.project_info.get("is_temporary"):
        return ""
    return str(runtime.project_info.get("name") or "").strip()


def _inline_click_output(command, args: list[str]) -> str:
    result = _InlineRunner().invoke(command, args)
    output = str(result.output or "").strip()
    if result.exit_code != 0:
        raise RuntimeError(output or "命令执行失败。")
    return output


def _parse_chat_command(command_text: str) -> _ParsedChatCommand:
    normalized = " ".join(str(command_text or "").strip().split())
    if not normalized:
        raise RuntimeError("空命令。")
    parts = normalized.split()
    return _ParsedChatCommand(raw=normalized, parts=parts, verb=parts[0].lower())


def _project_arg_or_active(parts: list[str], runtime: Any) -> str:
    return parts[1] if len(parts) > 1 else _active_chat_project_name(runtime)


def _emit_chat_message(message: str) -> str:
    click.echo(message)
    return message


def _execute_inline_chat_command(command, args: list[str]) -> str:
    output = _inline_click_output(command, args)
    click.echo(output)
    return output


def _switch_chat_project(runtime: Any, project_name: str, *, echo) -> str:
    runtime.project_info = runtime.shell.resolve_project_for_prompt(project_name)
    runtime.effective = runtime.shell._resolve_effective_options(
        runtime.project_info,
        planner=runtime.planner_opt,
        executor=runtime.executor_opt,
        auto_commit=runtime.auto_commit_opt,
        max_tasks=runtime.max_tasks_opt,
        max_retries=runtime.max_retries_opt,
    )
    runtime.default_agent = runtime.shell._resolve_task_agent(
        runtime.project_info,
        runtime.default_agent,
        runtime.effective["executor"],
    )
    message = f"已切换到项目 {runtime.project_info['name']}"
    echo(f"[green][OK] {message}[/green]")
    return message


def _render_chat_projects_summary() -> str:
    projects = db.list_projects()
    if not projects:
        return "当前还没有已注册项目。"
    lines = [f"已注册项目 {len(projects)} 个："]
    for project in projects:
        name = str(project.get("name") or "")
        stats = db.get_task_stats(name)
        lines.append(
            f"- {name}: backlog={stats['backlog']} in_progress={stats['in_progress']} failed={stats['failed']} done={stats['done']}"
        )
    return "\n".join(lines)


def _render_chat_overview(project_name: str) -> str:
    project = db.get_project(project_name)
    if not project:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    stats = db.get_task_stats(project_name)
    task_status = project_service_action(project_name, "tasks", "status").get("status") or {}
    inspect_status = project_service_action(project_name, "inspect", "status").get("status") or {}
    lines = [
        f"项目: {project_name}",
        f"路径: {project.get('path') or '-'}",
        f"任务: backlog={stats['backlog']} in_progress={stats['in_progress']} failed={stats['failed']} cancelled={stats['cancelled']} done={stats['done']} total={stats['total']}",
        f"任务执行服务: {'运行中' if task_status.get('running') else '未运行'} pid={task_status.get('pid') or 0}",
        f"巡检服务: {'运行中' if inspect_status.get('running') else '未运行'} pid={inspect_status.get('pid') or 0}",
    ]
    return "\n".join(lines)


def _render_chat_services(project_name: str) -> str:
    project = db.get_project(project_name)
    if not project:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    task_result = project_service_action(project_name, "tasks", "status")
    inspect_result = project_service_action(project_name, "inspect", "status")
    task_status = task_result.get("status") or {}
    inspect_status = inspect_result.get("status") or {}
    lines = [
        f"项目: {project_name}",
        f"任务执行服务: {'运行中' if task_status.get('running') else '未运行'} pid={task_status.get('pid') or 0}",
        f"巡检服务: {'运行中' if inspect_status.get('running') else '未运行'} pid={inspect_status.get('pid') or 0}",
    ]
    return "\n".join(lines)


def _render_chat_sessions(project_name: str) -> str:
    sessions = db.list_sessions(project=project_name)
    if not sessions:
        return f"{project_name} 当前没有需求会话。"
    lines = [f"{project_name} 最近需求会话："]
    for session in sessions[:8]:
        lines.append(
            f"- #{session['id']} {session.get('title') or '新会话'} [{session.get('status') or '-'}]"
        )
    return "\n".join(lines)


def _render_chat_workflow(project_name: str) -> str:
    from codepilot.commands.inspect_workflow import read_inspect_workflow_context
    from codepilot.commands.workflow import workflow_next_payload

    project = db.get_project(project_name)
    if not project:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    context = read_inspect_workflow_context(project) or {}
    quality = context.get("quality_summary") or {}
    actions = workflow_next_payload(project_name).get("next_actions") or []
    lines = [
        f"{project_name} 巡检工作流：",
        (
            "质量摘要: "
            f"created={quality.get('created_count', len(context.get('created_preview') or []))} "
            f"report_only={quality.get('report_only_count', len(context.get('report_only') or []))} "
            f"dropped={quality.get('dropped_count', len(context.get('dropped') or []))}"
        ),
    ]
    for item in (context.get("report_only") or [])[:4]:
        lines.append(f"- 报告项 {item.get('candidate_id')}: {item.get('title')}")
    lines.append(f"自动推进: workflow next {project_name} auto")
    if actions:
        lines.append("下一步：")
        for action in actions[:6]:
            lines.append(f"- {action.get('id')}: {action.get('label')}")
    else:
        lines.append("暂无 next_actions。")
    return "\n".join(lines)


def _run_catalog_chat_command(command: _ParsedChatCommand, runtime: Any, *, echo) -> str | None:
    if command.verb in {"projects", "global"}:
        return _emit_chat_message(_render_chat_projects_summary())
    return None


def _run_project_switch_chat_command(command: _ParsedChatCommand, runtime: Any, *, echo) -> str | None:
    if command.verb != "use":
        return None
    if len(command.parts) < 2:
        raise RuntimeError("缺少项目名。")
    return _switch_chat_project(runtime, command.parts[1], echo=echo)


def _run_project_view_chat_command(command: _ParsedChatCommand, runtime: Any, *, echo) -> str | None:
    if command.verb == "overview":
        return _emit_chat_message(_render_chat_overview(_project_arg_or_active(command.parts, runtime)))

    if command.verb == "tasks":
        project_name = _project_arg_or_active(command.parts, runtime)
        runtime.shell.render_project_dashboard(
            project_name,
            verbose=False,
            include_done=False,
            title=f"任务面板  {project_name}",
        )
        return f"已显示 {project_name} 任务面板"

    if command.verb in {"requirements", "sessions"}:
        return _emit_chat_message(_render_chat_sessions(_project_arg_or_active(command.parts, runtime)))

    if command.verb in {"workflow", "next"}:
        return _emit_chat_message(_render_chat_workflow(_project_arg_or_active(command.parts, runtime)))

    if command.verb == "services":
        return _emit_chat_message(_render_chat_services(_project_arg_or_active(command.parts, runtime)))

    return None


def _run_service_chat_command(command: _ParsedChatCommand, runtime: Any, *, echo) -> str | None:
    if command.verb not in {"daemon", "inspect"}:
        return None

    action = command.parts[1].lower() if len(command.parts) > 1 else "status"
    project_name = command.parts[2] if len(command.parts) > 2 else _active_chat_project_name(runtime)
    service_name = "tasks" if command.verb == "daemon" else "inspect"
    result = project_service_action(project_name, service_name, action)
    status = result.get("status") or {}
    service_label = "任务执行服务" if service_name == "tasks" else "巡检服务"
    message = (
        f"{project_name} {service_label}: "
        f"{result.get('message') or '-'} / {'运行中' if status.get('running') else '未运行'} / pid={status.get('pid') or 0}"
    )
    return _emit_chat_message(message)


def _run_task_proxy_chat_command(command: _ParsedChatCommand, runtime: Any, *, echo) -> str | None:
    from codepilot.commands import status as status_mod
    from codepilot.commands import tasks as tasks_mod

    single_id_commands = {
        "detail": tasks_mod.show,
        "logs": tasks_mod.logs,
        "retry": tasks_mod.retry,
        "stop": tasks_mod.stop,
    }
    if command.verb in single_id_commands:
        return _execute_inline_chat_command(single_id_commands[command.verb], [command.parts[1]])

    multi_id_commands = {
        "cancel": tasks_mod.cancel,
        "archive": tasks_mod.archive,
    }
    if command.verb in multi_id_commands:
        return _execute_inline_chat_command(multi_id_commands[command.verb], command.parts[1:])

    if command.verb == "delete":
        return _execute_inline_chat_command(tasks_mod.rm, [*command.parts[1:], "-f"])

    if command.verb == "status":
        return _execute_inline_chat_command(status_mod.status, ["-p", _project_arg_or_active(command.parts, runtime)])

    return None


_CHAT_COMMAND_HANDLERS: tuple[Callable[..., str | None], ...] = (
    _run_catalog_chat_command,
    _run_project_switch_chat_command,
    _run_project_view_chat_command,
    _run_service_chat_command,
    _run_task_proxy_chat_command,
)


def _execute_chat_command(command_text: str, runtime: Any, *, echo) -> str:
    command = _parse_chat_command(command_text)
    for handler in _CHAT_COMMAND_HANDLERS:
        result = handler(command, runtime, echo=echo)
        if result is not None:
            return result
    raise RuntimeError(f"未支持的自然语言操作: {command.raw}")


def _dispatch_chat_natural_language_command(
    text: str,
    runtime: Any,
    *,
    echo,
) -> tuple[bool, str]:
    selected = pick_command_option(text, runtime.pending_action_options)
    if selected:
        runtime.pending_action_options.clear()
        response = _execute_chat_command(str(selected.get("command") or ""), runtime, echo=echo)
        return True, response

    active_project = _active_chat_project_name(runtime)
    resolved = resolve_natural_language_command(text, active_project=active_project)
    if resolved.get("status") == "options":
        runtime.pending_action_options = list(resolved.get("options") or [])
        message = format_numbered_options(
            str(resolved.get("message") or "请确认操作"),
            runtime.pending_action_options,
        )
        click.echo(message)
        return True, message

    if resolved.get("status") == "match":
        runtime.pending_action_options.clear()
        response = _execute_chat_command(str(resolved.get("command") or ""), runtime, echo=echo)
        return True, response

    if runtime.pending_action_options and _normalize_nl_text(text).isdigit():
        message = format_numbered_options("可选项超出范围，请重新选择：", runtime.pending_action_options)
        click.echo(message)
        return True, message

    if runtime.pending_action_options:
        runtime.pending_action_options.clear()
    return False, ""
