"""MCP-backed chat command entrypoint."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from codepilot.ai_support.cli_families import env_var_for, get_family
from codepilot.commands.auto import run_chat_session
from codepilot.core.config import AgentsConfig, load_project_config
from codepilot.mcp.launchers import build_mcp_launch_plan
from codepilot.storage import database as db

SUPPORTED_CHAT_AGENTS = ("claude", "codex", "opencode")
DEFAULT_CHAT_AGENT = "opencode"
MCP_SERVER_SHUTDOWN_TIMEOUT_SECONDS = 5.0


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
            return value
    if cfg:
        configured = str((cfg.commands or {}).get(agent, "") or "").strip()
        if configured:
            return configured
    return agent


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


def _write_launch_config_files(config_files: dict[str, str], *, cwd: Path) -> None:
    for raw_path, content in config_files.items():
        path = Path(raw_path)
        if not path.is_absolute():
            path = cwd / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _launch_mcp_agent_chat(*, agent: str, project: str | None = None, prompt: str = "") -> int:
    record = _project_record(project)
    cfg = load_project_config(record or Path.cwd())
    cwd = Path(record["path"]).resolve() if record else Path.cwd().resolve()
    executable = _resolve_agent_executable(agent, cfg)
    plan = build_mcp_launch_plan(
        agent,
        executable=executable,
        prompt=prompt,
        mcp_servers=_codepilot_mcp_servers(project),
    )
    _write_launch_config_files(plan.config_files, cwd=cwd)
    env = os.environ.copy()
    env.update(plan.env)
    mcp_process = _start_codepilot_mcp_server(project=project, cwd=cwd, env=env)
    try:
        completed = subprocess.run(plan.command, cwd=str(cwd), env=env)
        return int(completed.returncode)
    finally:
        _stop_codepilot_mcp_server(mcp_process)


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
@click.option("--legacy", is_flag=True, help="使用旧版 classifier→plan→exec 交互会话")
@click.option("--planner", default=None, help="legacy 模式规划器；默认读取配置或使用 codex")
@click.option("--execute/--no-execute", default=None, help="legacy 模式默认是否自动执行；默认跟随配置")
@click.option(
    "--executor",
    type=click.Choice(["auto", "dispatch", "builtin"], case_sensitive=False),
    default=None,
    help="legacy 模式执行器类型；默认跟随配置",
)
@click.option("--auto-commit/--no-auto-commit", default=None, help="legacy 模式是否自动提交；默认跟随配置")
@click.option("--max-tasks", type=int, default=0, help="legacy 模式最大拆分任务数；0 表示读取配置")
@click.option("--max-retries", type=int, default=0, help="legacy 模式最大重试次数；0 表示读取配置")
@click.option("--legacy-classifier", is_flag=True, help="legacy 模式启用旧版独立意图分类器回退路径")
@click.option("--ui/--no-ui", "enable_ui", default=True, help="legacy 模式是否自动启动 Web UI（默认开启）")
@click.option("--ui-port", type=int, default=8766, help="legacy 模式 Web UI 端口")
@click.pass_context
def chat(
    ctx: click.Context,
    project: Optional[str],
    agent: Optional[str],
    legacy: bool,
    planner: Optional[str],
    execute: Optional[bool],
    executor: Optional[str],
    auto_commit: Optional[bool],
    max_tasks: int,
    max_retries: int,
    legacy_classifier: bool,
    enable_ui: bool,
    ui_port: int,
):
    """启动 MCP agent chat；传 --legacy 使用旧交互会话."""
    root_obj = _root_options(ctx)
    project = project or root_obj.get("direct_project")
    agent = agent or root_obj.get("agent")
    planner = planner or root_obj.get("planner")
    if execute is None:
        execute = root_obj.get("execute")
    executor = executor or root_obj.get("executor")
    if auto_commit is None:
        auto_commit = root_obj.get("auto_commit")
    if not max_tasks:
        max_tasks = root_obj.get("max_tasks", 0)
    if not max_retries:
        max_retries = root_obj.get("max_retries", 0)
    legacy_classifier = legacy_classifier or bool(root_obj.get("legacy_classifier", False))

    if legacy:
        run_chat_session(
            project=project,
            planner=planner,
            task_agent=agent,
            execute=execute,
            executor=executor,
            auto_commit=auto_commit,
            max_tasks=max_tasks,
            max_retries=max_retries,
            legacy_classifier=legacy_classifier,
            enable_ui=enable_ui,
            ui_port=ui_port,
        )
        return

    resolved_agent = _resolve_chat_agent(agent, project)
    try:
        exit_code = _launch_mcp_agent_chat(agent=resolved_agent, project=project, prompt="")
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    if exit_code:
        raise click.ClickException(f"chat agent exited with code {exit_code}")
