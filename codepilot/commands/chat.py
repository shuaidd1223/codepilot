"""MCP-backed chat command entrypoint."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, TextIO

import click

from codepilot.ai_support.cli_families import env_var_for, get_family
from codepilot.core.config import AgentsConfig, load_project_config
from codepilot.mcp.launchers import build_mcp_launch_plan
from codepilot.storage import database as db

SUPPORTED_CHAT_AGENTS = ("claude", "codex", "opencode")
DEFAULT_CHAT_AGENT = "opencode"
MCP_SERVER_SHUTDOWN_TIMEOUT_SECONDS = 5.0
AGENT_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class _PreparedChatLaunch:
    agent: str
    command: list[str]
    cwd: Path
    env: dict[str, str]
    mcp_process: subprocess.Popen


@dataclass(frozen=True)
class _RunningChatAgent:
    agent: str
    process: subprocess.Popen
    mcp_process: subprocess.Popen


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


def _codepilot_mcp_servers(project: str | None) -> dict[str, dict[str, object]]:
    command = _codepilot_mcp_command(project)
    return {
        "codepilot": {
            "command": command[0],
            "args": command[1:],
        }
    }


def _start_codepilot_mcp_server(
    *,
    project: str | None,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.Popen:
    return subprocess.Popen(
        _codepilot_mcp_command(project),
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _stop_codepilot_mcp_server(process: subprocess.Popen) -> None:
    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        return
    try:
        process.terminate()
    except Exception:
        _kill_codepilot_mcp_server(process)
        return
    try:
        process.wait(timeout=MCP_SERVER_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_codepilot_mcp_server(process)
    except Exception:
        _kill_codepilot_mcp_server(process)


def _kill_codepilot_mcp_server(process: subprocess.Popen) -> None:
    try:
        process.kill()
    except Exception:
        return
    try:
        process.wait(timeout=MCP_SERVER_SHUTDOWN_TIMEOUT_SECONDS)
    except Exception:
        return


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


def _prepare_mcp_agent_chat(*, agent: str, project: str | None, prompt: str) -> _PreparedChatLaunch:
    record = _project_record(project)
    cfg = load_project_config(record or Path.cwd())
    cwd = Path(record["path"]).resolve() if record else Path.cwd().resolve()
    server_project = project or (str(record.get("name") or "") if record else None)
    executable = _resolve_agent_executable(agent, cfg)
    plan = build_mcp_launch_plan(
        agent,
        executable=executable,
        prompt=prompt,
        mcp_servers=_codepilot_mcp_servers(server_project),
    )
    _write_launch_config_files(plan.config_files, cwd=cwd)
    env = os.environ.copy()
    env.update(plan.env)
    mcp_process = _start_codepilot_mcp_server(project=server_project, cwd=cwd, env=env)
    return _PreparedChatLaunch(
        agent=agent,
        command=plan.command,
        cwd=cwd,
        env=env,
        mcp_process=mcp_process,
    )


def _launch_mcp_agent_chat(*, agent: str, project: str | None = None, prompt: str = "") -> int:
    launch = _prepare_mcp_agent_chat(agent=agent, project=project, prompt=prompt)
    try:
        completed = subprocess.run(launch.command, cwd=str(launch.cwd), env=launch.env)
        return int(completed.returncode)
    finally:
        _stop_codepilot_mcp_server(launch.mcp_process)


def _start_mcp_agent_chat_process(
    *,
    agent: str,
    project: str | None,
    prompt: str,
) -> _RunningChatAgent:
    launch = _prepare_mcp_agent_chat(agent=agent, project=project, prompt=prompt)
    try:
        process = subprocess.Popen(
            launch.command,
            cwd=str(launch.cwd),
            env=launch.env,
            stdin=subprocess.PIPE,
            text=True,
        )
    except Exception:
        _stop_codepilot_mcp_server(launch.mcp_process)
        raise
    return _RunningChatAgent(
        agent=agent,
        process=process,
        mcp_process=launch.mcp_process,
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
        _stop_codepilot_mcp_server(running.mcp_process)


def _restart_running_chat_agent(running: _RunningChatAgent) -> None:
    _stop_chat_agent_process(running.process)
    _stop_codepilot_mcp_server(running.mcp_process)


def _run_mcp_agent_chat_session(
    *,
    agent: str,
    project: str | None = None,
    prompt: str = "",
    input_stream: Iterable[str] | TextIO | None = None,
) -> int:
    if input_stream is None:
        return _launch_mcp_agent_chat(agent=agent, project=project, prompt=prompt)

    current_agent = _normalize_chat_agent(agent)
    current_prompt = prompt
    while True:
        running = _start_mcp_agent_chat_process(
            agent=current_agent,
            project=project,
            prompt=current_prompt,
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
        except Exception:
            _restart_running_chat_agent(running)
            raise
        current_agent = next_agent
        current_prompt = ""


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
@click.pass_context
def chat(
    ctx: click.Context,
    project: Optional[str],
    agent: Optional[str],
):
    """启动 MCP agent chat (claude / codex / opencode)."""
    root_obj = _root_options(ctx)
    project = project or root_obj.get("direct_project")
    agent = agent or root_obj.get("agent")

    resolved_agent = _resolve_chat_agent(agent, project)
    try:
        input_stream = sys.stdin if getattr(sys.stdin, "isatty", lambda: False)() else None
        exit_code = _run_mcp_agent_chat_session(
            agent=resolved_agent,
            project=project,
            prompt="",
            input_stream=input_stream,
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    if exit_code:
        raise click.ClickException(f"chat agent exited with code {exit_code}")
