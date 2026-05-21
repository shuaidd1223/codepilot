"""Workflow mode state commands."""

from __future__ import annotations

from pathlib import Path

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.output import echo
from codepilot.core.workflow_state import get_agent_session, read_workflow_state
from codepilot.storage import database as db


@click.group("workflow")
def workflow_group() -> None:
    """查看工作流模式状态。"""


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


@click.command("status")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--mode", "-m", help="查看指定模式状态；不指定则查看 active workflow")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def status_cmd(ctx: click.Context, project: str | None, mode: str | None, json_mode: bool) -> None:
    """查看当前 active workflow 状态。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    project_path = str(project_info["path"])
    state = read_workflow_state(project_path, mode=mode)
    agent_session = get_agent_session(project_path)
    data = {
        "project": project_info["name"],
        "project_path": project_path,
        "mode": mode,
        "state": state,
        "agent_session": agent_session,
    }
    if json_mode:
        emit_json_payload("workflow status", ok=True, data=data)
        return

    if agent_session:
        click.echo("--- Agent Session ---")
        click.echo(f"session: {agent_session.get('session_id') or '-'}")
        click.echo(f"目标: {agent_session.get('goal') or '-'}")
        click.echo(f"当前阶段: {agent_session.get('current_phase') or '-'}")
        blocked = agent_session.get("blocked_reason")
        if blocked:
            click.echo(f"阻塞原因: {blocked}")
        actions = agent_session.get("next_actions") or []
        if actions:
            click.echo(f"下一步: {', '.join(actions)}")
        click.echo()

    if state is None:
        target = f"模式 {mode}" if mode else "active workflow"
        echo(f"[yellow]没有 {target} 状态[/yellow]")
        click.echo(f"项目: {project_info['name']}")
        click.echo(f"路径: {project_path}")
        return

    click.echo(f"项目: {project_info['name']}")
    click.echo(f"模式: {state.get('mode') or '-'}")
    click.echo(f"active: {state.get('active')}")
    click.echo(f"阶段: {state.get('current_phase') or '-'}")
    click.echo(f"session: {state.get('session_id') or '-'}")
    click.echo(f"context: {state.get('context_path') or '-'}")
    click.echo(f"updated_at: {state.get('updated_at') or '-'}")


workflow_group.add_command(status_cmd)
