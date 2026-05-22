"""Workflow actions shared by Web UI endpoints."""

from __future__ import annotations

from typing import Any

import click

from codepilot.storage import database as db


def run_inspect_workflow_action(
    project: str,
    *,
    max_new: int | None = None,
    planner: str | None = None,
    agent: str = "codex",
) -> dict[str, Any]:
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    from codepilot.commands.inspect_workflow import run_inspect_preview_to_workflow

    try:
        result = run_inspect_preview_to_workflow(
            project_info,
            max_new=max_new,
            planner=planner,
            agent=agent,
        )
    except click.ClickException as exc:
        raise RuntimeError(str(exc)) from exc
    return result


def execute_workflow_action(
    project: str,
    action_id: str,
    *,
    mode: str | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    from codepilot.commands.workflow import execute_workflow_next_action

    try:
        result = execute_workflow_next_action(
            project,
            action_id,
            mode=mode,
            allow_high_risk=allow_high_risk,
        )
    except click.ClickException as exc:
        raise RuntimeError(str(exc)) from exc
    return {"ok": True, **result}
