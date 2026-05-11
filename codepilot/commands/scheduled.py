"""Scheduled agent CLI commands."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.config import AgentsConfig, ScheduledAgentConfig, load_project_config
from codepilot.core.output import echo, safe
from codepilot.scheduled.guards import GUARDS_RELATIVE_PATH, agent_job_key
from codepilot.scheduled.runner import run_agent_job
from codepilot.scheduled.templates import render_prompt_template
from codepilot.storage import database as db


def _resolve_project(project: str | None) -> dict:
    db.init_db()
    if project:
        found = db.get_project(project)
        if not found:
            raise click.ClickException(f"项目 '{project}' 未注册。")
        return found
    found = db.find_project_by_path(Path.cwd())
    if not found:
        raise click.ClickException("当前目录不属于已注册项目；请使用 -p/--project 指定项目。")
    return found


def _project_root(project_info: Mapping[str, Any]) -> Path:
    path = str(project_info.get("path") or "").strip()
    if not path:
        raise click.ClickException("项目路径为空，无法读取 scheduled 配置。")
    return Path(path).expanduser().resolve()


def _project_name(project_info: Mapping[str, Any]) -> str:
    return str(project_info.get("name") or project_info.get("project") or "").strip()


def _load_agents_config(project_info: Mapping[str, Any]) -> AgentsConfig:
    config = load_project_config(project_info)
    if config is None:
        raise click.ClickException("未找到可用 AGENTS.toml 配置。")
    return config


def _redact_prompt_from_result(result: Any, prompt: str) -> dict[str, Any]:
    data = copy.deepcopy(result.to_dict())
    command = data.get("command")
    if isinstance(command, list):
        data["command"] = ["[prompt]" if item == prompt else item for item in command]
    return data


def _guards_path(project_root: Path) -> Path:
    return project_root / GUARDS_RELATIVE_PATH


def _load_guard_state(project_root: Path) -> dict[str, Any]:
    path = _guards_path(project_root)
    if not path.is_file():
        return {"schema_version": 1, "agents": {}, "daily_usage": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    agents = data.get("agents")
    daily_usage = data.get("daily_usage")
    return {
        "schema_version": 1,
        "agents": agents if isinstance(agents, dict) else {},
        "daily_usage": daily_usage if isinstance(daily_usage, dict) else {},
    }


def _save_guard_state(project_root: Path, state: dict[str, Any]) -> None:
    path = _guards_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _guard_key(project_name: str, name: str) -> str:
    return agent_job_key({"project": project_name, "name": name})


def _disabled_state(project_root: Path, project_name: str, name: str) -> dict[str, Any]:
    state = _load_guard_state(project_root)
    raw = state.get("agents", {}).get(_guard_key(project_name, name))
    return dict(raw) if isinstance(raw, dict) and raw.get("disabled") else {}


def _prompt_preview(prompt: str, limit: int = 40) -> str:
    text = str(prompt or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _agent_summary(
    name: str,
    cfg: ScheduledAgentConfig,
    *,
    project_root: Path,
    project_name: str,
    include_prompt_preview: bool = False,
) -> dict[str, Any]:
    disabled = _disabled_state(project_root, project_name, name)
    summary: dict[str, Any] = {
        "name": name,
        "agent": cfg.agent,
        "enabled": bool(cfg.enabled and not disabled),
        "configured_enabled": bool(cfg.enabled),
        "disabled": bool(disabled),
        "disabled_reason": str(disabled.get("disabled_reason") or "") if disabled else "",
        "interval": cfg.interval,
        "interval_seconds": cfg.interval_seconds,
        "schedule": cfg.schedule,
        "max_cost_usd": cfg.max_cost_usd,
        "max_daily_cost_usd": cfg.max_daily_cost_usd,
    }
    if include_prompt_preview:
        summary["prompt_preview"] = _prompt_preview(cfg.prompt)
        summary["prompt_length"] = len(cfg.prompt or "")
    return summary


def _get_scheduled_agent(config: AgentsConfig, name: str) -> ScheduledAgentConfig:
    try:
        return config.automation.scheduled_agents[name]
    except KeyError as exc:
        raise click.ClickException(f"未找到 scheduled agent：{name}") from exc


def _emit_error(ctx: click.Context, command: str, json_mode: bool, exc: Exception) -> None:
    if json_mode:
        emit_json_payload(command, ok=False, data={}, error=str(exc), error_code="scheduled_error")
        ctx.exit(1)
    raise click.ClickException(str(exc)) from exc


@click.group("scheduled")
def scheduled_group() -> None:
    """管理 scheduled agent 配置、手动执行和禁用状态。"""


@scheduled_group.command("list")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def list_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """列出 scheduled agents。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        project_root = _project_root(project_info)
        project_name = _project_name(project_info)
        config = _load_agents_config(project_info)
        agents = [
            _agent_summary(name, cfg, project_root=project_root, project_name=project_name)
            for name, cfg in sorted(config.automation.scheduled_agents.items())
        ]
    except (ValueError, click.ClickException) as exc:
        _emit_error(ctx, "scheduled list", json_mode, exc)
        return

    data = {"project": project_name, "agents": agents}
    if json_mode:
        emit_json_payload("scheduled list", ok=True, data=data)
        return
    if not agents:
        echo("[yellow]暂无 scheduled agents[/yellow]")
        return
    for agent in agents:
        cadence = agent.get("interval") or agent.get("schedule") or "-"
        status = "enabled" if agent["enabled"] else "disabled"
        click.echo(f"{agent['name']}\t{agent['agent']}\t{status}\t{cadence}")


@scheduled_group.command("show")
@click.argument("name")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def show_cmd(ctx: click.Context, name: str, project: str | None, json_mode: bool) -> None:
    """查看单个 scheduled agent 摘要。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        project_root = _project_root(project_info)
        project_name = _project_name(project_info)
        config = _load_agents_config(project_info)
        cfg = _get_scheduled_agent(config, name)
        agent = _agent_summary(
            name,
            cfg,
            project_root=project_root,
            project_name=project_name,
            include_prompt_preview=True,
        )
    except (ValueError, click.ClickException) as exc:
        _emit_error(ctx, "scheduled show", json_mode, exc)
        return

    data = {"project": project_name, "agent": agent}
    if json_mode:
        emit_json_payload("scheduled show", ok=True, data=data)
        return
    echo(f"[cyan]{safe(agent['name'])}[/cyan]  {safe(agent['agent'])}")
    click.echo(f"enabled: {agent['enabled']}")
    click.echo(f"cadence: {agent.get('interval') or agent.get('schedule') or '-'}")
    click.echo(f"prompt: {agent['prompt_preview']} ({agent['prompt_length']} chars)")


@scheduled_group.command("run-once")
@click.argument("name")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--dry-run", is_flag=True, help="只构造并审计执行，不调用外部 CLI")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def run_once_cmd(ctx: click.Context, name: str, project: str | None, dry_run: bool, json_mode: bool) -> None:
    """手动运行一个 scheduled agent。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        project_root = _project_root(project_info)
        project_name = _project_name(project_info)
        config = _load_agents_config(project_info)
        cfg = _get_scheduled_agent(config, name)
        now = datetime.now(timezone.utc)
        prompt = render_prompt_template(cfg.prompt, {"now": now.isoformat(), "project": project_name})
        job = {
            "type": "agent_job",
            "name": name,
            "project": project_name,
            "agent": cfg.agent,
            "prompt": prompt,
            "trigger": {"type": "manual"},
            "max_cost_usd": cfg.max_cost_usd,
            "max_daily_cost_usd": cfg.max_daily_cost_usd,
        }
        result = run_agent_job(
            job,
            project_root=project_root,
            dry_run=dry_run,
            commands=config.commands,
            config=config,
            now=now,
        )
    except (ValueError, click.ClickException) as exc:
        _emit_error(ctx, "scheduled run-once", json_mode, exc)
        return

    data = {
        "project": project_name,
        "job": {key: value for key, value in job.items() if key != "prompt"},
        "result": _redact_prompt_from_result(result, prompt),
    }
    if result.exit_code not in (None, 0):
        error = f"scheduled agent exited with code {result.exit_code}"
        if json_mode:
            emit_json_payload("scheduled run-once", ok=False, data=data, error=error, error_code="scheduled_run_failed")
            ctx.exit(int(result.exit_code))
        raise click.ClickException(error)
    if json_mode:
        emit_json_payload("scheduled run-once", ok=True, data=data)
        return
    status = result.guard_status if result.guard_status != "ok" else result.exit_code
    echo(f"[green]scheduled run-once 完成[/green]  {safe(name)}  status={safe(status)}")
    if result.audit_log_path:
        echo(f"[dim]audit: {safe(result.audit_log_path)}[/dim]")


@scheduled_group.command("disable")
@click.argument("name")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def disable_cmd(ctx: click.Context, name: str, project: str | None, json_mode: bool) -> None:
    """禁用一个 scheduled agent。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        project_root = _project_root(project_info)
        project_name = _project_name(project_info)
        config = _load_agents_config(project_info)
        cfg = _get_scheduled_agent(config, name)
        state = _load_guard_state(project_root)
        agents = state.setdefault("agents", {})
        if not isinstance(agents, dict):
            agents = {}
            state["agents"] = agents
        agents[_guard_key(project_name, name)] = {
            "disabled": True,
            "disabled_reason": "manual",
            "disabled_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_guard_state(project_root, state)
        agent = _agent_summary(
            name,
            cfg,
            project_root=project_root,
            project_name=project_name,
            include_prompt_preview=True,
        )
    except (ValueError, click.ClickException) as exc:
        _emit_error(ctx, "scheduled disable", json_mode, exc)
        return

    data = {"project": project_name, "agent": agent}
    if json_mode:
        emit_json_payload("scheduled disable", ok=True, data=data)
        return
    echo(f"[green][OK] 已禁用 scheduled agent[/green]  {safe(name)}")
