"""Project-local skill catalog commands."""

from __future__ import annotations

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.commands.status import _resolve_project
from codepilot.core import skill_catalog
from codepilot.core.output import echo, safe
from codepilot.storage import database as db


def _project_root(project: str) -> str:
    db.init_db()
    record = db.get_project(project)
    if not record:
        raise skill_catalog.SkillCatalogError(f"项目 {project} 未注册")
    return str(record["path"])


def _emit_error(ctx: click.Context, json_mode: bool, command: str, exc: Exception) -> None:
    if json_mode:
        emit_json_payload(command, ok=False, data={}, error=str(exc), error_code="skill_catalog_error")
        ctx.exit(1)
    raise click.ClickException(str(exc)) from exc


@click.group("skill")
def skill_group() -> None:
    """管理项目本地 skill catalog。"""


@skill_group.command("list")
@click.option("--project", "-p", callback=_resolve_project, required=True, help="项目名")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def list_cmd(ctx: click.Context, project: str, json_mode: bool) -> None:
    """列出本地技能。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        root = _project_root(project)
        skills = skill_catalog.list_skills(root)
    except skill_catalog.SkillCatalogError as exc:
        _emit_error(ctx, json_mode, "skill list", exc)
        return
    if json_mode:
        emit_json_payload("skill list", ok=True, data={"project": project, "skills": skills})
        return
    for skill in skills:
        marker = "on " if skill.get("enabled") else "off"
        echo(f"[cyan]{safe(skill['name'])}[/cyan]  {marker}  {safe(skill.get('description') or '')}")


@skill_group.command("search")
@click.argument("query")
@click.option("--project", "-p", callback=_resolve_project, required=True, help="项目名")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def search_cmd(ctx: click.Context, query: str, project: str, json_mode: bool) -> None:
    """搜索本地技能。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        root = _project_root(project)
        skills = skill_catalog.search_skills(root, query)
    except skill_catalog.SkillCatalogError as exc:
        _emit_error(ctx, json_mode, "skill search", exc)
        return
    if json_mode:
        emit_json_payload("skill search", ok=True, data={"project": project, "query": query, "skills": skills})
        return
    for skill in skills:
        echo(f"[cyan]{safe(skill['name'])}[/cyan]  {safe(skill.get('description') or '')}")


@skill_group.command("show")
@click.argument("name")
@click.option("--project", "-p", callback=_resolve_project, required=True, help="项目名")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def show_cmd(ctx: click.Context, name: str, project: str, json_mode: bool) -> None:
    """查看单个技能。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        root = _project_root(project)
        skill = skill_catalog.get_skill(root, name)
    except skill_catalog.SkillCatalogError as exc:
        _emit_error(ctx, json_mode, "skill show", exc)
        return
    if json_mode:
        emit_json_payload("skill show", ok=True, data={"project": project, "skill": skill})
        return
    echo(f"[cyan]{safe(skill['name'])}[/cyan]")
    click.echo(skill.get("description") or "")


def _toggle(ctx: click.Context, *, project: str, name: str, enabled: bool, json_mode: bool, command: str) -> None:
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        root = _project_root(project)
        skill = skill_catalog.set_skill_enabled(root, name, enabled)
    except skill_catalog.SkillCatalogError as exc:
        _emit_error(ctx, json_mode, command, exc)
        return
    if json_mode:
        emit_json_payload(command, ok=True, data={"project": project, "skill": skill})
        return
    status = "启用" if enabled else "禁用"
    echo(f"[green][OK] 已{status}技能[/green]  {safe(name)}")


@skill_group.command("enable")
@click.argument("name")
@click.option("--project", "-p", callback=_resolve_project, required=True, help="项目名")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def enable_cmd(ctx: click.Context, name: str, project: str, json_mode: bool) -> None:
    """启用本地技能。"""
    _toggle(ctx, project=project, name=name, enabled=True, json_mode=json_mode, command="skill enable")


@skill_group.command("disable")
@click.argument("name")
@click.option("--project", "-p", callback=_resolve_project, required=True, help="项目名")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def disable_cmd(ctx: click.Context, name: str, project: str, json_mode: bool) -> None:
    """禁用本地技能。"""
    _toggle(ctx, project=project, name=name, enabled=False, json_mode=json_mode, command="skill disable")
