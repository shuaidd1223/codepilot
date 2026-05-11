"""MCP-backed chat command entrypoint."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from io import StringIO
from importlib import import_module
from pathlib import Path
from typing import Iterable, Optional, TextIO

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from codepilot.ai_support.cli_families import env_var_for, get_family
from codepilot.core.config import AgentsConfig, load_project_config
from codepilot.mcp.launchers import build_mcp_launch_plan
from codepilot.mcp.server import _missing_mcp_sdk_message
from codepilot.opencode.env import build_agent_launch_env, build_opencode_config_from_agents_config, clean_agent_env
from codepilot.opencode.model_state import (
    load_latest_project_session_id,
    resolve_project_model_selection,
    sync_latest_project_model_selection,
)
from codepilot.opencode.paths import opencode_runtime_config_path, opencode_runtime_db_path
from codepilot.storage import database as db

SUPPORTED_CHAT_AGENTS = ("claude", "codex", "opencode")
DEFAULT_CHAT_AGENT = "opencode"
AGENT_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class _PreparedChatLaunch:
    agent: str
    command: list[str]
    cwd: Path
    env: dict[str, str]
    opencode_db_path: Path | None = None
    opencode_scope: str = ""
    requested_session: str = ""


@dataclass(frozen=True)
class _RunningChatAgent:
    agent: str
    process: subprocess.Popen
    launch: _PreparedChatLaunch


def _root_options(ctx: click.Context) -> dict:
    root = ctx.find_root()
    return root.obj if root and root.obj else {}


def _project_record(project: str | None) -> dict | None:
    db.init_db()
    if project:
        record = db.get_project(project)
        if not record:
            raise click.ClickException(f"项目 '{project}' 未注册")
        return record
    return db.find_project_by_path(Path.cwd())


def _load_chat_config(project: str | None) -> AgentsConfig | None:
    record = _project_record(project)
    return load_project_config(record or Path.cwd())


def _normalize_chat_agent(agent: str) -> str:
    family = get_family(agent)
    if family is None or family.name not in SUPPORTED_CHAT_AGENTS:
        supported = ", ".join(SUPPORTED_CHAT_AGENTS)
        raise click.ClickException(f"不支持的 chat agent: {agent!r}。支持: {supported}")
    return family.name


def _resolve_chat_agent(agent: str | None, project: str | None) -> str:
    if agent:
        return _normalize_chat_agent(agent)
    cfg = _load_chat_config(project)
    configured = ""
    if cfg and getattr(cfg, "automation", None):
        configured = str(getattr(cfg.automation, "default_agent", "") or "").strip()
    return _normalize_chat_agent(configured or DEFAULT_CHAT_AGENT)


def _resolve_agent_executable(agent: str, cfg: AgentsConfig | None) -> str:
    env_var = env_var_for(agent)
    if env_var:
        value = os.environ.get(env_var, "").strip()
        if value:
            return _resolve_executable_path(value)
    if cfg:
        configured = str((cfg.commands or {}).get(agent, "") or "").strip()
        if configured:
            return _resolve_executable_path(configured)
    return _resolve_executable_path(agent)


def _resolve_executable_path(value: str) -> str:
    resolved = shutil.which(value)
    return resolved or value


def _codepilot_mcp_command(project: str | None) -> list[str]:
    command: list[str] = [
        sys.executable,
        "-m",
        "codepilot",
        "mcp",
        "serve",
        "--transport",
        "stdio",
    ]
    if project:
        command.extend(["--project", project])
    return command


def _codepilot_source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _pythonpath_with_source_root(source_root: Path) -> str:
    existing = os.environ.get("PYTHONPATH", "").strip()
    if not existing:
        return str(source_root)
    entries = [str(source_root), *[item for item in existing.split(os.pathsep) if item]]
    return os.pathsep.join(dict.fromkeys(entries))


def _codepilot_mcp_servers(
    project: str | None,
    *,
    source_root: Path | None = None,
) -> dict[str, dict[str, object]]:
    command = _codepilot_mcp_command(project)
    root = Path(source_root or _codepilot_source_root()).resolve()
    return {
        "codepilot": {
            "command": command[0],
            "args": command[1:],
            "env": {"PYTHONPATH": _pythonpath_with_source_root(root)},
        }
    }


def _ensure_mcp_sdk_available() -> None:
    try:
        import_module("mcp.server.fastmcp")
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing == "mcp" or missing.startswith("mcp."):
            raise click.ClickException(_missing_mcp_sdk_message()) from exc
        raise


def _chat_agent_label(agent: str) -> str:
    labels = {
        "claude": "Claude",
        "codex": "Codex",
        "opencode": "OpenCode",
    }
    return labels.get(agent, agent)


def _chat_launch_label(launch: _PreparedChatLaunch) -> str:
    if launch.agent == "opencode":
        brand_name = str(launch.env.get("CODEPILOT_OPENCODE_BRAND_NAME") or "").strip()
        if brand_name:
            return brand_name
    return _chat_agent_label(launch.agent)


def _set_terminal_title(title: str) -> None:
    if not title or not bool(getattr(sys.stdout, "isatty", lambda: False)()):
        return
    try:
        sys.stdout.write(f"\x1b]0;{title}\x07")
        sys.stdout.flush()
    except Exception:
        return


def _reset_terminal_after_tui() -> None:
    if not bool(getattr(sys.stdout, "isatty", lambda: False)()):
        return
    # Defensive cleanup for TUIs that leave mouse/focus reporting enabled.
    sequence = "".join(
        (
            "\x1b[?1000l",  # X10 mouse
            "\x1b[?1002l",  # button-event mouse
            "\x1b[?1003l",  # any-event mouse
            "\x1b[?1005l",  # UTF-8 mouse
            "\x1b[?1006l",  # SGR mouse
            "\x1b[?1015l",  # urxvt mouse
            "\x1b[?1004l",  # focus events
            "\x1b[?25h",  # show cursor
            "\x1b[0m",  # reset style
        )
    )
    try:
        sys.stdout.write(sequence)
        sys.stdout.flush()
    except Exception:
        return


def _clear_terminal_screen_for_codepilot() -> None:
    """Clear stale startup/TUI output before CodePilot renders its own panel."""
    sequence = "".join(
        (
            "\x1b[?1000l",
            "\x1b[?1002l",
            "\x1b[?1003l",
            "\x1b[?1005l",
            "\x1b[?1006l",
            "\x1b[?1015l",
            "\x1b[?1004l",
            "\x1b[?25h",
            "\x1b[0m",
            "\x1b[2J\x1b[H",
        )
    )
    wrote = False
    for stream in (sys.stderr, sys.stdout):
        if not bool(getattr(stream, "isatty", lambda: False)()):
            continue
        try:
            stream.write(sequence)
            stream.flush()
            wrote = True
        except Exception:
            continue
    if wrote:
        return


def _print_codepilot_resume_hint(launch: _PreparedChatLaunch, *, project: str | None) -> None:
    if launch.agent != "opencode" or launch.opencode_db_path is None:
        return
    session_id = launch.requested_session or load_latest_project_session_id(
        launch.cwd,
        db_path=launch.opencode_db_path,
    )
    if not session_id:
        return
    command = _codepilot_resume_command(session_id, project=project)
    click.echo(_codepilot_resume_panel(command, session_id=session_id), err=True)


def _replace_opencode_exit_screen(launch: _PreparedChatLaunch) -> None:
    if launch.agent != "opencode":
        return
    if not bool(getattr(sys.stdout, "isatty", lambda: False)()):
        return
    try:
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
    except Exception:
        return


def _codepilot_resume_command(session_id: str, *, project: str | None = None) -> str:
    parts = ["codepilot", "chat"]
    if project:
        parts.extend(["--project", project])
    parts.extend(["--session", session_id])
    return " ".join(parts)


def _codepilot_resume_panel(command: str, *, session_id: str = "") -> str:
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", style="cyan", no_wrap=True)
    table.add_column(style="white")
    if session_id:
        table.add_row("会话", Text(session_id, style="bold green"))
    table.add_row("恢复", Text(command, style="bold"))
    output = StringIO()
    console = Console(
        file=output,
        width=88,
        force_terminal=False,
        color_system=None,
        highlight=False,
        legacy_windows=False,
        safe_box=False,
    )
    console.print(
        Panel(
            table,
            title="CodePilot 会话已暂停",
            border_style="cyan",
            expand=False,
        )
    )
    return output.getvalue().rstrip()


def _stderr_is_tty() -> bool:
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


@contextmanager
def _chat_startup_status(message: str):
    if _stderr_is_tty():
        console = Console(file=sys.stderr, highlight=False)
        with console.status(f"[cyan]{message}[/cyan]", spinner="dots"):
            yield
        return

    yield


def _chat_startup_notice(message: str) -> None:
    if _stderr_is_tty():
        try:
            sys.stderr.write(_chat_startup_frame(message, fill=8, phase=0))
            sys.stderr.flush()
        except Exception:
            return
        return
    click.echo(message, err=True)


def _clear_chat_startup_notice() -> None:
    if not _stderr_is_tty():
        return
    try:
        sys.stderr.write("\x1b[?25h\x1b[0m\x1b[?1049l")
        sys.stderr.flush()
    except Exception:
        return


def _chat_startup_frame(message: str, *, fill: int, phase: int) -> str:
    fill = max(0, min(fill, 8))
    bar = "█" * fill + "░" * (8 - fill)
    dots = "." * ((phase % 3) + 1)
    return "\n".join(
        [
            "\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H",
            "",
            "        _____ ____  _____  ______ _____ _____ _      ____ _______",
            "       / ____/ __ \\|  __ \\|  ____|  __ \\_   _| |    / __ \\__   __|",
            "      | |   | |  | | |  | | |__  | |__) || | | |   | |  | | | |",
            "      | |   | |  | | |  | |  __| |  ___/ | | | |   | |  | | | |",
            "      | |___| |__| | |__| | |____| |    _| |_| |___| |__| | | |",
            "       \\_____\\____/|_____/|______|_|   |_____|______\\____/  |_|",
            "",
            "                                      CODEPILOT",
            "                         项目工作流智能体正在接入 OpenCode",
            f"                         {bar}  {message}{dots}",
            "",
        ]
    )


def _stop_chat_agent_process(process: subprocess.Popen) -> None:
    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        return
    try:
        process.terminate()
    except Exception:
        _kill_chat_agent_process(process)
        return
    try:
        process.wait(timeout=AGENT_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_chat_agent_process(process)
    except Exception:
        _kill_chat_agent_process(process)


def _kill_chat_agent_process(process: subprocess.Popen) -> None:
    try:
        process.kill()
    except Exception:
        return
    try:
        process.wait(timeout=AGENT_SHUTDOWN_TIMEOUT_SECONDS)
    except Exception:
        return


def _close_chat_agent_stdin(process: subprocess.Popen) -> None:
    stdin = getattr(process, "stdin", None)
    if stdin is None:
        return
    try:
        stdin.close()
    except Exception:
        return


def _write_launch_config_files(config_files: dict[str, str], *, cwd: Path) -> None:
    for raw_path, content in config_files.items():
        path = Path(raw_path)
        if not path.is_absolute():
            path = cwd / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _prepare_mcp_agent_chat(
    *,
    agent: str,
    project: str | None,
    prompt: str,
    session: str | None = None,
) -> _PreparedChatLaunch:
    record = _project_record(project)
    cfg = load_project_config(record or Path.cwd())
    cwd = Path(record["path"]).resolve() if record else Path.cwd().resolve()
    server_project = str(record.get("name") or "") if record else None
    _ensure_mcp_sdk_available()
    executable = _resolve_agent_executable(agent, cfg)
    family = get_family(agent)
    runtime_scope = server_project or cwd.name
    is_opencode = family is not None and family.name == "opencode"
    source_root = _codepilot_source_root()
    opencode_db = opencode_runtime_db_path(runtime_scope) if is_opencode else None
    session_id = str(session or "").strip()
    config_path = (
        opencode_runtime_config_path(runtime_scope)
        if is_opencode
        else None
    )
    plan = build_mcp_launch_plan(
        agent,
        executable=executable,
        prompt=prompt,
        mcp_servers=_codepilot_mcp_servers(server_project, source_root=source_root),
        config_path=config_path,
        opencode_config=build_opencode_config_from_agents_config(
            cfg,
            preferred_model=resolve_project_model_selection(runtime_scope, cwd, db_path=opencode_db),
        )
        if is_opencode
        else None,
        opencode_session=session_id if is_opencode else None,
    )
    if plan.config_files:
        _write_launch_config_files(plan.config_files, cwd=cwd)
    env = os.environ.copy()
    env.update(plan.env)
    configured_keys = build_agent_launch_env(agent, cfg)
    env.update(configured_keys)
    clean_agent_env(env, cfg)
    return _PreparedChatLaunch(
        agent=agent,
        command=plan.command,
        cwd=cwd,
        env=env,
        opencode_db_path=opencode_db,
        opencode_scope=runtime_scope if is_opencode else "",
        requested_session=session_id,
    )


def _prepare_chat_launch_with_feedback(
    *,
    agent: str,
    project: str | None,
    prompt: str,
    session: str | None = None,
) -> _PreparedChatLaunch:
    try:
        with _chat_startup_status("正在准备 CodePilot MCP 工具..."):
            return _prepare_mcp_agent_chat(agent=agent, project=project, prompt=prompt, session=session)
    except Exception:
        click.echo("CodePilot MCP 配置准备失败。", err=True)
        raise


def _launch_mcp_agent_chat(
    *,
    agent: str,
    project: str | None = None,
    prompt: str = "",
    session: str | None = None,
) -> int:
    launch = _prepare_chat_launch_with_feedback(agent=agent, project=project, prompt=prompt, session=session)
    label = _chat_launch_label(launch)
    _set_terminal_title(label)
    _chat_startup_notice(f"正在启动 {label} TUI，初始化 MCP 可能需要几秒...")
    _clear_chat_startup_notice()
    try:
        completed = subprocess.run(launch.command, cwd=str(launch.cwd), env=launch.env)
        return int(completed.returncode)
    finally:
        _sync_opencode_model_selection(launch)
        _reset_terminal_after_tui()
        _print_codepilot_resume_hint(launch, project=project)


def _start_mcp_agent_chat_process(
    *,
    agent: str,
    project: str | None,
    prompt: str,
    session: str | None = None,
) -> _RunningChatAgent:
    launch = _prepare_chat_launch_with_feedback(agent=agent, project=project, prompt=prompt, session=session)
    try:
        label = _chat_launch_label(launch)
        _set_terminal_title(label)
        _chat_startup_notice(f"正在启动 {label} TUI，初始化 MCP 可能需要几秒...")
        _clear_chat_startup_notice()
        process = subprocess.Popen(
            launch.command,
            cwd=str(launch.cwd),
            env=launch.env,
            stdin=subprocess.PIPE,
            text=True,
        )
    except Exception:
        raise
    return _RunningChatAgent(
        agent=agent,
        process=process,
        launch=launch,
    )


def _parse_agent_switch(line: str) -> str | None:
    parts = line.strip().split()
    if len(parts) == 2 and parts[0] == "/agent":
        return parts[1]
    return None


def _write_agent_input(process: subprocess.Popen, line: str) -> bool:
    stdin = getattr(process, "stdin", None)
    if stdin is None:
        return False
    try:
        stdin.write(line)
        stdin.flush()
    except (BrokenPipeError, OSError):
        return False
    return True


def _finish_running_chat_agent(running: _RunningChatAgent) -> int:
    _close_chat_agent_stdin(running.process)
    try:
        return int(running.process.wait())
    finally:
        _sync_opencode_model_selection(running.launch)
        _reset_terminal_after_tui()


def _restart_running_chat_agent(running: _RunningChatAgent) -> None:
    try:
        _stop_chat_agent_process(running.process)
    finally:
        _sync_opencode_model_selection(running.launch)
        _reset_terminal_after_tui()


def _sync_opencode_model_selection(launch: _PreparedChatLaunch) -> None:
    if launch.agent != "opencode" or not launch.opencode_scope or launch.opencode_db_path is None:
        return
    try:
        sync_latest_project_model_selection(
            launch.opencode_scope,
            launch.cwd,
            db_path=launch.opencode_db_path,
        )
    except Exception:
        return


def _run_mcp_agent_chat_session(
    *,
    agent: str,
    project: str | None = None,
    prompt: str = "",
    session: str | None = None,
    input_stream: Iterable[str] | TextIO | None = None,
) -> int:
    if input_stream is None:
        return _launch_mcp_agent_chat(agent=agent, project=project, prompt=prompt, session=session)

    current_agent = _normalize_chat_agent(agent)
    current_prompt = prompt
    while True:
        running = _start_mcp_agent_chat_process(
            agent=current_agent,
            project=project,
            prompt=current_prompt,
            session=session if current_agent == "opencode" else None,
        )
        next_agent: str | None = None
        try:
            for line in input_stream:
                requested_agent = _parse_agent_switch(line)
                if requested_agent is None:
                    if not _write_agent_input(running.process, line):
                        return _finish_running_chat_agent(running)
                    continue
                try:
                    normalized_agent = _normalize_chat_agent(requested_agent)
                except click.ClickException as exc:
                    click.echo(exc.format_message(), err=True)
                    continue
                if normalized_agent == current_agent:
                    continue
                next_agent = normalized_agent
                click.echo(
                    f"切换 chat agent: {current_agent} -> {next_agent}",
                    err=True,
                )
                _restart_running_chat_agent(running)
                break
            if next_agent is None:
                return _finish_running_chat_agent(running)
        except KeyboardInterrupt:
            _restart_running_chat_agent(running)
            raise
        except Exception:
            _restart_running_chat_agent(running)
            raise
        current_agent = next_agent
        current_prompt = ""
        session = None


@click.command("chat")
@click.option("--project", help="项目名称；不指定则自动识别当前项目")
@click.option(
    "--agent",
    "-a",
    "agent",
    type=click.Choice(SUPPORTED_CHAT_AGENTS, case_sensitive=False),
    default=None,
    help="启动的 MCP chat agent；默认读取 [automation].default_agent，否则 opencode",
)
@click.option(
    "--session",
    "-s",
    "session",
    default=None,
    help="恢复 CodePilot 隔离 OpenCode 会话 ID",
)
@click.pass_context
def chat(
    ctx: click.Context,
    project: Optional[str],
    agent: Optional[str],
    session: Optional[str] = None,
):
    """启动 MCP agent chat (claude / codex / opencode)."""
    root_obj = _root_options(ctx)
    project = project or root_obj.get("direct_project")
    agent = agent or root_obj.get("agent")
    session = session or root_obj.get("chat_session")

    resolved_agent = _resolve_chat_agent(agent, project)
    try:
        input_stream = None if session or getattr(sys.stdin, "isatty", lambda: False)() else sys.stdin
        launch_kwargs = {
            "agent": resolved_agent,
            "project": project,
            "prompt": "",
            "input_stream": input_stream,
        }
        if session:
            launch_kwargs["session"] = session
        exit_code = _run_mcp_agent_chat_session(**launch_kwargs)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    if exit_code:
        raise click.ClickException(f"chat agent exited with code {exit_code}")
