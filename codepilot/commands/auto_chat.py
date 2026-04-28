"""Interactive chat REPL for CodePilot.

Owns :func:`run_chat_session` and its helpers. Imported and re-exported by
:mod:`codepilot.commands.auto` so historical ``from codepilot.commands.auto import
run_chat_session`` / direct attribute access paths keep working.

Dependencies that tests monkeypatch on the shell module (``classify_intent``,
``answer_question_via_api``, ``run_requirement_workflow``, ``_project_config``,
``render_project_dashboard``, ``render_project_stats``) are looked up at call
time via ``codepilot.commands.auto``.
"""

from __future__ import annotations

import http.client
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional

import click
from click.testing import CliRunner as _InlineRunner

from codepilot.ai_support.interaction_controller import (
    interpret_clarification_outcome,
    parse_intent_prefix as _parse_intent_prefix_core,
    resolve_turn_intent,
    should_continue_pending_clarification,
)
from codepilot import __version__
from codepilot.core.runtime import no_window_kwargs
from codepilot.nl_command_router import (
    _normalize_text as _normalize_nl_text,
    format_numbered_options,
    pick_command_option,
    resolve_natural_language_command,
)
from codepilot.storage import database as db
from codepilot.webapp.action_task_ops import project_service_action


def _shell():
    """Return the ``codepilot.commands.auto`` module for dynamic attribute lookup."""
    return sys.modules["codepilot.commands.auto"]


def _chat_help() -> str:
    return "\n".join(
        [
            "会话命令：",
            "  /help               查看帮助",
            "  /version            查看当前版本",
            "  /exit               退出会话",
            "  /status             查看任务看板（/status watch 实时刷新）",
            "  /stats              查看状态统计",
            "  /history            查看对话记录",
            "  /clear              清空对话历史",
            "  /project <name>     切换项目",
            "  /agent <name>       切换默认任务智能体（如 codex / claude / dual）",
            "  /execute on|off     切换默认是否自动执行",
            "  /plan               只规划下一条需求，不执行",
            "  /run                自动执行下一条需求",
            "",
            "任务管理：",
            "  /cancel <id ...>    取消任务（保留记录）",
            "  /resume <id ...>    恢复 cancelled/failed 任务到 backlog",
            "  /stop <id>          停止运行中的任务",
            "  /retry <id>         重试任务",
            "  /rm <id ...>        删除任务",
            "  /logs <id>          查看任务日志",
            "",
            "输入前缀（跳过自动分类）：",
            "  ? <文本>            当作问题直接回答，不建任务",
            "  ! <文本>            当作单任务，不拆分",
            "  # <文本>            当作需求，强制拆分",
            "",
            "会话内会自动记住上下文，连续提问无需重复说明。",
        ]
    )


def _parse_intent_prefix(text: str) -> tuple[Optional[str], str]:
    """Return (forced_intent, stripped_text). forced_intent ∈ {question,task,requirement} 或 None."""
    return _parse_intent_prefix_core(text)


class _Spinner:
    """Simple inline spinner for long-running operations."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "思考中"):
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stderr.write(f"\r  {frame} {self._message}...")
            sys.stderr.flush()
            i += 1
            self._stop.wait(0.1)


def _start_chat_ui(port: int = 8766):
    """Ensure Web UI is running for chat mode without blocking REPL startup."""
    from codepilot.core.output import echo, safe

    if _is_chat_ui_healthy(port):
        echo(f"[dim]Web UI 已在运行: http://127.0.0.1:{port}/[/dim]")
        return {"managed": False, "port": int(port)}

    cmd = [
        sys.executable,
        "-m",
        "codepilot",
        "ui",
        "start",
        "--no-open",
        "--no-daemon",
        "--port",
        str(int(port)),
    ]
    try:
        popen_kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        # Keep the launcher isolated from chat Ctrl+C / session lifecycle.
        popen_kwargs.update(no_window_kwargs(new_process_group=True))
        proc = subprocess.Popen(
            cmd,
            **popen_kwargs,
        )
    except Exception as exc:
        echo(f"[yellow]Web UI 启动失败：{safe(exc)}[/yellow]")
        return None

    echo(f"[dim]Web UI 启动请求已发送: http://127.0.0.1:{port}/[/dim]")
    return {"managed": True, "port": int(port), "launcher_pid": int(proc.pid)}


def _is_chat_ui_healthy(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """Fast local health probe to avoid redundant startup calls."""
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _stop_chat_ui(_handle) -> None:
    """No-op: Web UI is a global shared singleton service.

    Chat only ensures the service is running, but must not stop it on exit,
    otherwise other terminals/processes using the same UI would be disrupted.
    """
    return


class _ChatLoopState(Enum):
    """Discrete states for the chat REPL loop."""

    READ_INPUT = auto()
    DISPATCH = auto()
    HANDLE_COMMAND = auto()
    HANDLE_PENDING_CLARIFICATION = auto()
    HANDLE_FREE_TEXT = auto()
    EXIT = auto()


@dataclass
class _ChatRuntime:
    """Mutable runtime context shared across chat loop states."""

    shell: object
    project_info: dict
    effective: dict
    default_execute: bool
    default_agent: str
    planner_opt: Optional[str]
    executor_opt: Optional[str]
    auto_commit_opt: Optional[bool]
    max_tasks_opt: int
    max_retries_opt: int
    chat_history: list[dict] = field(default_factory=list)
    pending_clarification: Optional[dict] = None
    pending_action_options: list[dict] = field(default_factory=list)


@dataclass
class _ChatTurnFrame:
    """Per-turn frame that drives state transitions in the REPL."""

    state: _ChatLoopState = _ChatLoopState.READ_INPUT
    raw: str = ""
    text: str = ""
    forced_intent: Optional[str] = None
    payload_text: str = ""


@dataclass
class _ChatMessageDispatchContext:
    """Session-driven dispatch context for one free-text chat turn."""

    runtime: _ChatRuntime
    payload_text: str
    forced_intent: Optional[str]
    shared_gateway_options: object
    intent: str = "requirement"


@dataclass
class _ChatLoopDispatchContext:
    """Runtime bundle used by the chat loop state dispatcher."""

    frame: _ChatTurnFrame
    runtime: _ChatRuntime
    shutdown_ui: object
    echo: object
    safe: object


_CHAT_INTENT_LABELS = {
    "question": "正在检索上下文并回答",
    "task": "正在评估并执行任务",
    "requirement": "正在评估并规划需求",
    "command": "正在识别命令输入",
}

_CHAT_TASK_COMMAND_MAP = {
    "/cancel": "cancel",
    "/resume": "resume",
    "/retry": "retry",
    "/rm": "rm",
    "/stop": "stop",
    "/logs": "logs",
}


def _active_chat_project_name(runtime: _ChatRuntime) -> str:
    if runtime.project_info.get("is_temporary"):
        return ""
    return str(runtime.project_info.get("name") or "").strip()


def _inline_click_output(command, args: list[str]) -> str:
    result = _InlineRunner().invoke(command, args)
    output = str(result.output or "").strip()
    if result.exit_code != 0:
        raise RuntimeError(output or "命令执行失败。")
    return output


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


def _execute_chat_command(command_text: str, runtime: _ChatRuntime, *, echo) -> str:
    from codepilot.commands import status as status_mod
    from codepilot.commands import tasks as tasks_mod

    command_text = " ".join(str(command_text or "").strip().split())
    if not command_text:
        raise RuntimeError("空命令。")
    parts = command_text.split()
    verb = parts[0].lower()

    if verb == "projects":
        message = _render_chat_projects_summary()
        click.echo(message)
        return message

    if verb == "global":
        message = _render_chat_projects_summary()
        click.echo(message)
        return message

    if verb == "use":
        if len(parts) < 2:
            raise RuntimeError("缺少项目名。")
        runtime.project_info = runtime.shell.resolve_project_for_prompt(parts[1])
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

    if verb == "overview":
        project_name = parts[1] if len(parts) > 1 else _active_chat_project_name(runtime)
        message = _render_chat_overview(project_name)
        click.echo(message)
        return message

    if verb == "tasks":
        project_name = parts[1] if len(parts) > 1 else _active_chat_project_name(runtime)
        runtime.shell.render_project_dashboard(
            project_name,
            verbose=False,
            include_done=False,
            title=f"任务面板  {project_name}",
        )
        return f"已显示 {project_name} 任务面板"

    if verb in {"requirements", "sessions"}:
        project_name = parts[1] if len(parts) > 1 else _active_chat_project_name(runtime)
        message = _render_chat_sessions(project_name)
        click.echo(message)
        return message

    if verb == "services":
        project_name = parts[1] if len(parts) > 1 else _active_chat_project_name(runtime)
        message = _render_chat_services(project_name)
        click.echo(message)
        return message

    if verb in {"daemon", "inspect"}:
        action = parts[1].lower() if len(parts) > 1 else "status"
        project_name = parts[2] if len(parts) > 2 else _active_chat_project_name(runtime)
        service_name = "tasks" if verb == "daemon" else "inspect"
        result = project_service_action(project_name, service_name, action)
        status = result.get("status") or {}
        message = (
            f"{project_name} {('任务执行服务' if service_name == 'tasks' else '巡检服务')}: "
            f"{result.get('message') or '-'} / {'运行中' if status.get('running') else '未运行'} / pid={status.get('pid') or 0}"
        )
        click.echo(message)
        return message

    if verb == "detail":
        output = _inline_click_output(tasks_mod.show, [parts[1]])
        click.echo(output)
        return output

    if verb == "logs":
        output = _inline_click_output(tasks_mod.logs, [parts[1]])
        click.echo(output)
        return output

    if verb == "retry":
        output = _inline_click_output(tasks_mod.retry, [parts[1]])
        click.echo(output)
        return output

    if verb == "stop":
        output = _inline_click_output(tasks_mod.stop, [parts[1]])
        click.echo(output)
        return output

    if verb == "cancel":
        output = _inline_click_output(tasks_mod.cancel, parts[1:])
        click.echo(output)
        return output

    if verb == "archive":
        output = _inline_click_output(tasks_mod.archive, parts[1:])
        click.echo(output)
        return output

    if verb == "delete":
        output = _inline_click_output(tasks_mod.rm, [*parts[1:], "-f"])
        click.echo(output)
        return output

    if verb == "status":
        project_name = parts[1] if len(parts) > 1 else _active_chat_project_name(runtime)
        output = _inline_click_output(status_mod.status, ["-p", project_name])
        click.echo(output)
        return output

    raise RuntimeError(f"未支持的自然语言操作: {command_text}")


def _dispatch_chat_natural_language_command(
    text: str,
    runtime: _ChatRuntime,
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


def _end_chat_session(frame: _ChatTurnFrame, *, shutdown_ui, echo, prepend_blank_line: bool = False) -> None:
    """Stop chat loop and emit the unified session-end message."""
    if prepend_blank_line:
        echo()
    shutdown_ui()
    echo("[dim]会话已结束[/dim]")
    frame.state = _ChatLoopState.EXIT


def _reset_to_read_input(frame: _ChatTurnFrame) -> None:
    """Move the chat loop back to read-input state."""
    frame.state = _ChatLoopState.READ_INPUT


def _handle_chat_project_runtime_command(cmd: str, parts: list[str], runtime: _ChatRuntime, *, echo) -> bool:
    """Handle project/session-scoped slash commands."""
    shell = runtime.shell

    if cmd == "/status":
        if runtime.project_info.get("is_temporary"):
            echo("[yellow]临时会话没有任务看板。请先 /project <已注册项目> 或在当前目录执行 codepilot init。[/yellow]")
            return True
        watch = len(parts) > 1 and parts[1].lower() in ("watch", "live", "-w")
        if watch:
            echo("[dim]实时刷新中，按 Ctrl+C 停止...[/dim]")
        try:
            while True:
                if watch:
                    click.clear()
                shell.render_project_dashboard(
                    runtime.project_info["name"],
                    verbose=False,
                    include_done=False,
                    title=f"任务面板  {runtime.project_info['name']}",
                )
                if not watch:
                    break
                time.sleep(3)
        except KeyboardInterrupt:
            if watch:
                echo("\n[dim]已停止刷新[/dim]")
        return True

    if cmd == "/stats":
        if runtime.project_info.get("is_temporary"):
            echo("[yellow]临时会话没有项目统计。请先 /project <已注册项目> 或在当前目录执行 codepilot init。[/yellow]")
            return True
        shell.render_project_stats(runtime.project_info["name"], title=f"状态统计  {runtime.project_info['name']}")
        return True

    if cmd == "/project":
        if len(parts) < 2:
            echo("[yellow]用法: /project <name>[/yellow]")
            return True
        runtime.project_info = shell.resolve_project_for_prompt(parts[1])
        runtime.effective = shell._resolve_effective_options(
            runtime.project_info,
            planner=runtime.planner_opt,
            executor=runtime.executor_opt,
            auto_commit=runtime.auto_commit_opt,
            max_tasks=runtime.max_tasks_opt,
            max_retries=runtime.max_retries_opt,
        )
        runtime.default_agent = shell._resolve_task_agent(
            runtime.project_info,
            runtime.default_agent,
            runtime.effective["executor"],
        )
        echo(f"[green][OK] 已切换项目[/green] {runtime.project_info['name']}")
        return True

    if cmd == "/agent":
        if len(parts) < 2:
            echo("[yellow]用法: /agent <name>[/yellow]")
            return True
        try:
            runtime.default_agent = shell._resolve_task_agent(
                runtime.project_info,
                parts[1],
                runtime.effective["executor"],
            )
        except click.ClickException as exc:
            echo(f"[red]{exc.format_message()}[/red]")
            return True
        echo(f"[green][OK] 默认任务智能体已设置为 {runtime.default_agent}[/green]")
        return True

    if cmd == "/execute":
        if len(parts) < 2 or parts[1].lower() not in {"on", "off"}:
            echo("[yellow]用法: /execute on|off[/yellow]")
            return True
        runtime.default_execute = parts[1].lower() == "on"
        echo(f"[green][OK] 自动执行已设置为 {runtime.default_execute}[/green]")
        return True

    if cmd == "/plan":
        runtime.default_execute = False
        echo("[green][OK] 下一条需求将只规划不执行[/green]")
        return True

    if cmd == "/run":
        runtime.default_execute = True
        echo("[green][OK] 下一条需求将自动执行[/green]")
        return True

    return False


def _handle_chat_task_command(cmd: str, parts: list[str], *, echo) -> bool:
    """Handle slash commands that proxy to task management CLI commands."""
    target_name = _CHAT_TASK_COMMAND_MAP.get(cmd)
    if not target_name:
        return False

    ids = [int(x) for x in parts[1:] if x.isdigit()]
    if not ids:
        echo(f"[yellow]用法: {cmd} <task_id ...>[/yellow]")
        return True

    from click.testing import CliRunner as _InlineRunner
    from codepilot.commands import tasks as tasks_cmd_mod

    target_cmd = getattr(tasks_cmd_mod, target_name)
    args = [str(i) for i in ids]
    if cmd == "/rm":
        args.append("-f")
    _InlineRunner().invoke(target_cmd, args)
    return True


def _handle_chat_meta_command(
    cmd: str,
    runtime: _ChatRuntime,
    frame: _ChatTurnFrame,
    *,
    shutdown_ui,
    echo,
) -> bool:
    """Handle chat-local meta commands that do not touch workflow state."""
    if cmd == "/exit":
        _end_chat_session(frame, shutdown_ui=shutdown_ui, echo=echo)
        return True
    if cmd == "/help":
        click.echo(_chat_help())
        return True
    if cmd == "/version":
        click.echo(f"CodePilot {__version__}")
        return True
    if cmd == "/history":
        if not runtime.chat_history:
            echo("[dim]暂无对话记录[/dim]")
        else:
            for i, turn in enumerate(runtime.chat_history, 1):
                intent_tag = turn.get("intent", "?")
                echo(f"[dim]#{i}[/dim] [{intent_tag}] {turn['user'][:80]}")
                if turn.get("assistant"):
                    click.echo(f"  → {turn['assistant'][:120]}")
        return True
    if cmd in {"/clear", "/cancel"}:
        runtime.chat_history.clear()
        runtime.pending_action_options.clear()
        if runtime.pending_clarification:
            runtime.pending_clarification = None
            echo("[green]对话历史已清空，当前待澄清的需求也已放弃[/green]")
        else:
            echo("[green]对话历史已清空[/green]")
        return True
    return False


def _read_turn_input(frame: _ChatTurnFrame, *, shutdown_ui, echo) -> None:
    """Read one line and advance to the dispatch state when input is non-empty."""
    try:
        raw = click.prompt("codepilot", prompt_suffix="> ", default="", show_default=False)
    except (EOFError, KeyboardInterrupt, click.Abort):
        _end_chat_session(frame, shutdown_ui=shutdown_ui, echo=echo, prepend_blank_line=True)
        return

    text = raw.strip()
    if not text:
        frame.state = _ChatLoopState.READ_INPUT
        return

    frame.raw = raw
    frame.text = text
    frame.forced_intent = None
    frame.payload_text = ""
    frame.state = _ChatLoopState.DISPATCH


def _dispatch_turn(frame: _ChatTurnFrame, runtime: _ChatRuntime) -> None:
    """Route input to command/pending-clarification/free-text handlers."""
    if frame.text.startswith("/"):
        frame.state = _ChatLoopState.HANDLE_COMMAND
        return

    forced_intent, payload_text = _parse_intent_prefix(frame.text)
    frame.forced_intent = forced_intent
    frame.payload_text = payload_text
    if should_continue_pending_clarification(
        pending_state=runtime.pending_clarification,
        forced_intent=forced_intent,
        raw_text=frame.text,
        category="auto",
    ):
        frame.state = _ChatLoopState.HANDLE_PENDING_CLARIFICATION
        return
    frame.state = _ChatLoopState.HANDLE_FREE_TEXT


def _handle_chat_command(frame: _ChatTurnFrame, runtime: _ChatRuntime, *, shutdown_ui, echo) -> None:
    """Execute one slash command and transition back to input state."""
    parts = frame.text.split()
    cmd = parts[0].lower()

    if _handle_chat_meta_command(
        cmd,
        runtime,
        frame,
        shutdown_ui=shutdown_ui,
        echo=echo,
    ):
        if frame.state != _ChatLoopState.EXIT:
            _reset_to_read_input(frame)
        return

    if _handle_chat_project_runtime_command(cmd, parts, runtime, echo=echo):
        _reset_to_read_input(frame)
        return

    if _handle_chat_task_command(cmd, parts, echo=echo):
        _reset_to_read_input(frame)
        return

    echo("[yellow]未知会话命令[/yellow]")
    click.echo(_chat_help())
    _reset_to_read_input(frame)


def _handle_pending_clarification_turn(frame: _ChatTurnFrame, runtime: _ChatRuntime, *, shutdown_ui, echo, safe) -> None:
    """Continue an in-flight clarification dialog and possibly trigger planning."""
    shell = runtime.shell
    pending_state = runtime.pending_clarification or {}
    pending_intent = pending_state.get("intent", "requirement")
    prompt_status, clarify_answers, answer_text = shell._prompt_clarification_answers_for_cli(
        pending_state.get("last_questions") or [],
        first_input=frame.payload_text,
        allow_skip=False,
    )
    if prompt_status == "cancel":
        runtime.pending_clarification = None
        echo("[yellow]已取消当前这次需求规划[/yellow]")
        click.echo()
        frame.state = _ChatLoopState.READ_INPUT
        return
    if prompt_status == "empty":
        echo("[yellow]请至少回答一个澄清问题，或输入 /clear 取消当前规划。[/yellow]")
        click.echo()
        frame.state = _ChatLoopState.READ_INPUT
        return

    spinner = _Spinner("正在评估补充信息")
    spinner.__enter__()
    outcome = shell.continue_pending_clarification(
        pending_state,
        answer=answer_text,
        clarify_answers=clarify_answers,
        project_info=runtime.project_info,
        planner=runtime.effective["planner"],
    )
    spinner.__exit__(None, None, None)

    transition = interpret_clarification_outcome(
        outcome,
        pending_state=pending_state,
        fallback_title=answer_text,
        default_error_message="澄清评估失败",
        normalize_text=shell.normalize_requirement_text,
    )

    if transition.status == "interrupt":
        _end_chat_session(frame, shutdown_ui=shutdown_ui, echo=echo, prepend_blank_line=True)
        return

    if transition.status == "error":
        message = transition.message or "澄清评估失败"
        echo(f"[red]{safe(message)}[/red]")
        runtime.chat_history.append({
            "user": answer_text,
            "assistant": f"错误: {message}",
            "intent": "clarify",
        })
        click.echo()
        frame.state = _ChatLoopState.READ_INPUT
        return

    if transition.status == "needs_clarification":
        runtime.pending_clarification = transition.pending_state or pending_state
        questions = list(transition.questions)
        echo("[cyan]还需要再澄清一下：[/cyan]")
        click.echo(shell.render_clarification_questions(questions))
        runtime.chat_history.append({
            "user": answer_text,
            "assistant": "继续澄清：\n" + shell.render_clarification_questions(questions),
            "intent": "clarify",
        })
        click.echo()
        frame.state = _ChatLoopState.READ_INPUT
        return

    # Ready — take refined title forward into planning.
    refined = transition.refined_title or pending_state.get("original_title") or answer_text
    runtime.pending_clarification = None
    echo(f"[green][OK] 已澄清需求：{refined}[/green]")

    try:
        _run_chat_requirement_workflow(runtime, title=refined, intent=pending_intent)
        runtime.chat_history.append({
            "user": answer_text,
            "assistant": "需求已规划并执行",
            "intent": pending_intent,
        })
    except KeyboardInterrupt:
        _end_chat_session(frame, shutdown_ui=shutdown_ui, echo=echo, prepend_blank_line=True)
        return
    except click.ClickException as exc:
        echo(f"[red]{safe(exc.format_message())}[/red]")
        runtime.chat_history.append({
            "user": answer_text,
            "assistant": f"错误: {exc.format_message()}",
            "intent": pending_intent,
        })
    except Exception as exc:
        echo(f"[red]{safe(exc)}[/red]")
        runtime.chat_history.append({
            "user": answer_text,
            "assistant": f"错误: {exc}",
            "intent": pending_intent,
        })
    click.echo()
    _reset_to_read_input(frame)


def _chat_max_tasks_for_intent(runtime: _ChatRuntime, intent: str) -> int:
    return 1 if intent == "task" else runtime.effective["max_tasks"]


def _run_chat_requirement_workflow(runtime: _ChatRuntime, *, title: str, intent: str) -> None:
    shell = runtime.shell
    shell.run_requirement_workflow(
        project_info=runtime.project_info,
        title=title,
        planner=runtime.effective["planner"],
        task_agent=runtime.default_agent,
        execute=runtime.default_execute,
        executor=runtime.effective["executor"],
        auto_commit=runtime.effective["auto_commit"],
        max_tasks=_chat_max_tasks_for_intent(runtime, intent),
        max_retries=runtime.effective["max_retries"],
        quiet=True,
    )


def _resolve_chat_turn_intent(ctx: _ChatMessageDispatchContext) -> str:
    return resolve_turn_intent(
        ctx.payload_text,
        category="auto",
        forced_intent=ctx.forced_intent,
        classify_fn=ctx.runtime.shell.classify_entry_intent,
        classify_kwargs={
            "project_info": ctx.runtime.project_info,
            "category": "auto",
            "gateway_options": ctx.shared_gateway_options,
        },
        fallback_intent="question",
    )


def _dispatch_chat_qa_or_command(
    ctx: _ChatMessageDispatchContext,
    *,
    spinner: _Spinner,
    echo,
) -> str:
    shell = ctx.runtime.shell
    if ctx.intent == "command":
        spinner.__exit__(None, None, None)
        echo("[yellow]这看起来是在调用 codepilot 自身命令，请直接用下面的入口：[/yellow]")
        click.echo(shell.command_intent_guidance(include_release=True))
        click.echo()
        return "请使用对应的 CLI 命令操作"

    echo("[dim]阶段 2/2：正在检索上下文并回答...[/dim]")
    answer = shell.answer_question_via_api(
        provider_key=ctx.shared_gateway_options.classifier_provider,
        question=ctx.payload_text,
        gateway_options=ctx.shared_gateway_options,
        history=ctx.runtime.chat_history,
    )
    spinner.__exit__(None, None, None)
    if answer:
        click.echo(answer)
        return answer
    echo("[yellow]未获得回答[/yellow]")
    return ""


def _dispatch_chat_requirement(
    ctx: _ChatMessageDispatchContext,
    *,
    spinner: _Spinner,
    echo,
) -> str:
    runtime = ctx.runtime
    shell = runtime.shell
    echo("[dim]阶段 2/3：正在评估需求完整度...[/dim]")
    spinner._message = "正在评估需求完整度"
    assessment = shell.assess_requirement_for_planning(
        ctx.payload_text,
        project_info=runtime.project_info,
        planner=runtime.effective["planner"],
    )
    spinner.__exit__(None, None, None)

    runtime.pending_clarification = shell.clarification_state_from_assessment(
        assessment=assessment,
        seed_title=ctx.payload_text,
        intent=ctx.intent,
    )
    if runtime.pending_clarification:
        questions = runtime.pending_clarification.get("last_questions") or []
        echo("[cyan]为了更好地规划，我想先确认几个点：[/cyan]")
        click.echo(shell.render_clarification_questions(questions))
        echo("[dim]下一条输入会作为澄清回答；选项题可输入编号/标签，也可直接输入其他文本。输入 /clear 或 /cancel 放弃此需求。[/dim]")
        return "请求澄清：\n" + shell.render_clarification_questions(questions)

    refined = assessment.get("refined_title") or ctx.payload_text
    echo("[dim]阶段 3/3：正在生成计划并执行任务...[/dim]")
    spinner._message = "正在生成计划并执行任务"
    _run_chat_requirement_workflow(runtime, title=refined, intent=ctx.intent)
    return "任务已创建并执行" if ctx.intent == "task" else "需求已规划"


def _chat_requirement_confirmation_message(intent: str) -> str:
    label = "任务" if intent == "task" else "需求"
    prefix = "任务" if intent == "task" else "需求"
    symbol = "!" if intent == "task" else "#"
    return (
        f"这条消息更像要创建{label}，但当前不会直接执行。"
        f"请明确输入 `{prefix} <内容>` 或 `{symbol} <内容>` 再继续。"
    )


def _handle_free_text_turn(frame: _ChatTurnFrame, runtime: _ChatRuntime, *, shutdown_ui, echo, safe) -> None:
    """Run intent classification + question/requirement handling for normal input."""
    payload_text = frame.payload_text
    handled, assistant_response = _dispatch_chat_natural_language_command(payload_text, runtime, echo=echo)
    if handled:
        runtime.chat_history.append({
            "user": payload_text,
            "assistant": assistant_response,
            "intent": "command",
        })
        click.echo()
        _reset_to_read_input(frame)
        return

    dispatch_ctx = _ChatMessageDispatchContext(
        runtime=runtime,
        payload_text=payload_text,
        forced_intent=frame.forced_intent,
        shared_gateway_options=runtime.shell.resolve_shared_gateway_options(runtime.project_info),
    )

    echo("[dim]阶段 1/3：正在识别输入意图...[/dim]")
    spinner = _Spinner("正在识别输入意图")
    spinner.__enter__()

    dispatch_ctx.intent = _resolve_chat_turn_intent(dispatch_ctx)
    spinner._message = _CHAT_INTENT_LABELS.get(dispatch_ctx.intent, "正在处理中")

    assistant_response = ""
    try:
        if dispatch_ctx.intent in {"command", "question"}:
            assistant_response = _dispatch_chat_qa_or_command(
                dispatch_ctx,
                spinner=spinner,
                echo=echo,
            )
        else:
            if frame.forced_intent is None:
                spinner.__exit__(None, None, None)
                assistant_response = _chat_requirement_confirmation_message(dispatch_ctx.intent)
                echo(f"[yellow]{assistant_response}[/yellow]")
                click.echo()
                runtime.chat_history.append({
                    "user": payload_text,
                    "assistant": assistant_response,
                    "intent": "confirm",
                })
                _reset_to_read_input(frame)
                return
            assistant_response = _dispatch_chat_requirement(
                dispatch_ctx,
                spinner=spinner,
                echo=echo,
            )
    except KeyboardInterrupt:
        spinner.__exit__(None, None, None)
        _end_chat_session(frame, shutdown_ui=shutdown_ui, echo=echo, prepend_blank_line=True)
        return
    except click.ClickException as exc:
        spinner.__exit__(None, None, None)
        echo(f"[red]{safe(exc.format_message())}[/red]")
        assistant_response = f"错误: {exc.format_message()}"
    except Exception as exc:
        spinner.__exit__(None, None, None)
        echo(f"[red]{safe(exc)}[/red]")
        assistant_response = f"错误: {exc}"

    runtime.chat_history.append({
        "user": payload_text,
        "assistant": assistant_response,
        "intent": dispatch_ctx.intent or "unknown",
    })
    click.echo()
    _reset_to_read_input(frame)


def _dispatch_chat_loop_read_input(ctx: _ChatLoopDispatchContext) -> None:
    _read_turn_input(ctx.frame, shutdown_ui=ctx.shutdown_ui, echo=ctx.echo)


def _dispatch_chat_loop_turn_router(ctx: _ChatLoopDispatchContext) -> None:
    _dispatch_turn(ctx.frame, ctx.runtime)


def _dispatch_chat_loop_command(ctx: _ChatLoopDispatchContext) -> None:
    _handle_chat_command(ctx.frame, ctx.runtime, shutdown_ui=ctx.shutdown_ui, echo=ctx.echo)


def _dispatch_chat_loop_pending_clarification(ctx: _ChatLoopDispatchContext) -> None:
    _handle_pending_clarification_turn(
        ctx.frame,
        ctx.runtime,
        shutdown_ui=ctx.shutdown_ui,
        echo=ctx.echo,
        safe=ctx.safe,
    )


def _dispatch_chat_loop_free_text(ctx: _ChatLoopDispatchContext) -> None:
    _handle_free_text_turn(
        ctx.frame,
        ctx.runtime,
        shutdown_ui=ctx.shutdown_ui,
        echo=ctx.echo,
        safe=ctx.safe,
    )


_CHAT_LOOP_DISPATCHERS: dict[_ChatLoopState, Callable[[_ChatLoopDispatchContext], None]] = {
    _ChatLoopState.READ_INPUT: _dispatch_chat_loop_read_input,
    _ChatLoopState.DISPATCH: _dispatch_chat_loop_turn_router,
    _ChatLoopState.HANDLE_COMMAND: _dispatch_chat_loop_command,
    _ChatLoopState.HANDLE_PENDING_CLARIFICATION: _dispatch_chat_loop_pending_clarification,
    _ChatLoopState.HANDLE_FREE_TEXT: _dispatch_chat_loop_free_text,
}


def _step_chat_loop(ctx: _ChatLoopDispatchContext) -> None:
    """Run one loop step for the current chat state."""
    handler = _CHAT_LOOP_DISPATCHERS.get(ctx.frame.state)
    if handler is None:
        ctx.frame.state = _ChatLoopState.EXIT
        return
    handler(ctx)


def run_chat_session(
    *,
    project: Optional[str] = None,
    planner: Optional[str] = None,
    task_agent: Optional[str] = None,
    execute: Optional[bool] = None,
    executor: Optional[str] = None,
    auto_commit: Optional[bool] = None,
    max_tasks: int = 0,
    max_retries: int = 0,
    enable_ui: bool = True,
    ui_port: int = 8766,
) -> None:
    """Run a simple REPL that accepts plain-text requirements."""
    from codepilot.core.output import echo, safe

    shell = _shell()
    project_info = shell.resolve_project_for_prompt(
        project,
        auto_register=False,
        allow_temporary=True,
    )
    effective = shell._resolve_effective_options(
        project_info,
        planner=planner,
        executor=executor,
        auto_commit=auto_commit,
        max_tasks=max_tasks,
        max_retries=max_retries,
    )
    default_execute = effective["auto_execute"] if execute is None else execute
    default_agent = shell._resolve_task_agent(project_info, task_agent, effective["executor"])

    ui_handle = None
    if enable_ui:
        ui_handle = _start_chat_ui(ui_port)

    runtime = _ChatRuntime(
        shell=shell,
        project_info=project_info,
        effective=effective,
        default_execute=default_execute,
        default_agent=default_agent,
        planner_opt=planner,
        executor_opt=executor,
        auto_commit_opt=auto_commit,
        max_tasks_opt=max_tasks,
        max_retries_opt=max_retries,
    )

    echo(
        f"[cyan]CodePilot Chat[/cyan]  项目: {runtime.project_info['name']}  "
        f"planner={runtime.effective['planner']} executor={runtime.effective['executor']} agent={runtime.default_agent}"
    )
    if runtime.project_info.get("is_temporary"):
        echo(
            "[yellow]当前为公共临时会话：可继续问答；如需创建需求/任务，请先在目标目录执行 codepilot init，"
            "或使用 /project 切换到已注册项目。[/yellow]"
        )
    echo("[dim]直接输入文本即可。问题会直接回答，需求会自动规划执行。[/dim]")
    echo("[dim]输入 /help 查看命令，/history 查看对话记录。[/dim]")
    echo()

    def _shutdown_ui():
        _stop_chat_ui(ui_handle)

    frame = _ChatTurnFrame()
    dispatch_ctx = _ChatLoopDispatchContext(
        frame=frame,
        runtime=runtime,
        shutdown_ui=_shutdown_ui,
        echo=echo,
        safe=safe,
    )
    while frame.state != _ChatLoopState.EXIT:
        _step_chat_loop(dispatch_ctx)

