"""Workflow mode state commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.config import resolve_project_config_reference
from codepilot.core.output import echo
from codepilot.core.workflow_state import get_agent_session, read_workflow_state, workflow_dirs
from codepilot.storage import database as db


@click.group("workflow")
def workflow_group() -> None:
    """查看和安全推进工作流模式状态。"""


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


def _emit_error(ctx: click.Context, command: str, json_mode: bool, exc: Exception) -> None:
    if json_mode:
        emit_json_payload(command, ok=False, data={}, error=str(exc), error_code="workflow_next_error")
        ctx.exit(1)
    raise click.ClickException(str(exc))


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_project_file(project_path: Path, raw_path: str | Path | None, *, label: str) -> Path | None:
    if raw_path is None or str(raw_path).strip() == "":
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = project_path / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(project_path):
        raise click.ClickException(f"{label} 必须指向项目目录内的文件：{resolved}")
    return resolved


def _read_context(project_path: Path, raw_path: str | Path | None) -> tuple[Path | None, dict[str, Any]]:
    context_path = _resolve_project_file(project_path, raw_path, label="workflow context")
    if context_path is None or not context_path.is_file():
        return context_path, {}
    return context_path, _read_json_file(context_path) or {}


def _iter_mode_states(project_path: Path) -> list[dict[str, Any]]:
    state_dir = workflow_dirs(project_path)["state"]
    if not state_dir.is_dir():
        return []
    states: list[dict[str, Any]] = []
    for path in sorted(state_dir.glob("*-state.json")):
        state = _read_json_file(path)
        if not state:
            continue
        state["_state_path"] = str(path)
        states.append(state)
    return states


def _state_sort_key(state: dict[str, Any]) -> tuple[str, str]:
    return (str(state.get("updated_at") or state.get("started_at") or ""), str(state.get("_state_path") or ""))


def _latest_workflow_state(project_path: Path) -> dict[str, Any] | None:
    active = read_workflow_state(project_path)
    if active:
        return dict(active)
    states = _iter_mode_states(project_path)
    if not states:
        return None
    return dict(max(states, key=_state_sort_key))


def _normalize_next_actions(raw_actions: Any) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not isinstance(raw_actions, list):
        return actions
    for item in raw_actions:
        if isinstance(item, dict):
            action_id = str(item.get("id") or "").strip()
            if not action_id:
                continue
            action = dict(item)
            action["id"] = action_id
            action["label"] = str(action.get("label") or action_id)
            action["risk"] = str(action.get("risk") or "unknown").lower()
            actions.append(action)
        elif isinstance(item, str) and item.strip():
            text = item.strip()
            actions.append({"id": text, "label": text, "risk": "unknown", "suggested_command": ""})
    return actions


def _format_next_actions(raw_actions: Any) -> list[str]:
    labels: list[str] = []
    for action in _normalize_next_actions(raw_actions):
        action_id = str(action.get("id") or "").strip()
        label = str(action.get("label") or "").strip()
        if label and label != action_id:
            labels.append(f"{action_id} ({label})")
        elif action_id:
            labels.append(action_id)
    return labels


def _load_next_context(project_info: dict, *, mode: str | None = None) -> dict[str, Any]:
    project_path = Path(project_info["path"]).resolve()
    state = read_workflow_state(project_path, mode=mode) if mode else _latest_workflow_state(project_path)
    if state:
        context_path, context = _read_context(project_path, state.get("context_path"))
        next_actions = _normalize_next_actions(context.get("next_actions") or state.get("next_actions") or [])
        return {
            "type": "workflow_state",
            "state": state,
            "context": context,
            "context_path": str(context_path) if context_path else "",
            "next_actions": next_actions,
        }
    if mode:
        raise click.ClickException(f"没有找到模式 {mode} 的 next_actions。")

    agent_session = get_agent_session(project_path)
    if agent_session:
        artifacts = agent_session.get("artifact_paths") or {}
        context_path, context = _read_context(project_path, artifacts.get("context"))
        next_actions = _normalize_next_actions(
            context.get("next_actions")
            or agent_session.get("next_action_details")
            or agent_session.get("next_actions")
            or []
        )
        return {
            "type": "agent_session",
            "state": agent_session,
            "context": context,
            "context_path": str(context_path) if context_path else "",
            "next_actions": next_actions,
        }

    target = f"模式 {mode}" if mode else "最新 workflow"
    raise click.ClickException(f"没有找到 {target} 的 next_actions。")


def _source_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    state = bundle.get("state") or {}
    return {
        "type": bundle.get("type"),
        "mode": state.get("mode"),
        "session_id": state.get("session_id"),
        "context_path": bundle.get("context_path") or state.get("context_path") or "",
    }


def _find_next_action(bundle: dict[str, Any], action_id: str) -> dict[str, Any]:
    wanted = str(action_id or "").strip()
    for action in bundle.get("next_actions") or []:
        if action.get("id") == wanted:
            return dict(action)
    raise click.ClickException(f"未找到 next_action：{wanted}")


def _artifact_value(bundle: dict[str, Any], key: str) -> str | Path | None:
    context = bundle.get("context") or {}
    for payload in (context, context.get("state") or {}, bundle.get("state") or {}):
        if key == "spec" and payload.get("artifact_path"):
            return payload.get("artifact_path")
        if key == "task_batch" and payload.get("task_batch_path"):
            return payload.get("task_batch_path")
        artifacts = payload.get("artifact_paths") or {}
        if artifacts.get(key):
            return artifacts.get(key)
    return None


def _execute_plan_from_spec(project_info: dict, bundle: dict[str, Any]) -> dict[str, Any]:
    from codepilot.commands.plan import _read_spec, _summary_from_spec, write_plan_artifact

    project_path = Path(project_info["path"]).resolve()
    spec_path = _resolve_project_file(project_path, _artifact_value(bundle, "spec"), label="clarify spec")
    if spec_path is None:
        raise click.ClickException("当前 workflow context 没有可用于 plan_from_spec 的 clarify spec。")
    resolved_spec, spec_text = _read_spec(project_path, str(spec_path))
    summary = _summary_from_spec(spec_text)
    return write_plan_artifact(
        project_info,
        summary,
        source="spec",
        source_path=str(resolved_spec),
        use_wiki=True,
    )


def _execute_import_tasks(project_info: dict, bundle: dict[str, Any]) -> dict[str, Any]:
    from codepilot.commands.add import import_task_batch_file

    project_path = Path(project_info["path"]).resolve()
    task_batch_path = _resolve_project_file(project_path, _artifact_value(bundle, "task_batch"), label="task batch")
    if task_batch_path is None:
        raise click.ClickException("当前 workflow context 没有 task_batch_path，不能导入任务。")
    if not task_batch_path.is_file():
        raise click.ClickException(f"任务批次文件不存在：{task_batch_path}")

    imported = import_task_batch_file(
        str(project_info["name"]),
        task_batch_path,
        agent="codex",
        priority="P2",
        dep_list=None,
        proj_path=str(project_path),
        config_ref=resolve_project_config_reference(project_info),
    )
    return {
        "task_batch_path": str(task_batch_path),
        "count": len(imported),
        "imported": imported,
    }


_ALLOWED_NEXT_ACTIONS = {
    "plan_from_spec": _execute_plan_from_spec,
    "import_tasks": _execute_import_tasks,
}


def _execute_next_action(
    project_info: dict,
    bundle: dict[str, Any],
    action: dict[str, Any],
    *,
    allow_high_risk: bool,
) -> dict[str, Any]:
    action_id = str(action.get("id") or "").strip()
    risk = str(action.get("risk") or "").lower()
    if risk == "high" and not allow_high_risk:
        raise click.ClickException(
            f"next_action `{action_id}` 是高风险动作，默认拒绝执行；确认后请显式添加 --allow-high-risk。"
        )
    handler = _ALLOWED_NEXT_ACTIONS.get(action_id)
    if handler is None:
        allowed = ", ".join(sorted(_ALLOWED_NEXT_ACTIONS))
        raise click.ClickException(f"不支持的 next_action：{action_id}。当前 allowlist：{allowed}。")
    return handler(project_info, bundle)


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
        actions = _format_next_actions(agent_session.get("next_action_details") or agent_session.get("next_actions"))
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


@click.command("next")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--mode", "-m", help="从指定 workflow mode 读取 next_actions；不指定则读取最新 workflow")
@click.option("--list", "list_actions", is_flag=True, help="只列出可用 next_actions，不执行")
@click.option("--action", "action_id", help="执行指定 next_actions id（仅支持安全 allowlist）")
@click.option("--allow-high-risk", is_flag=True, help="显式允许高风险动作通过风险检查；动作仍必须在 allowlist 内")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def next_cmd(
    ctx: click.Context,
    project: str | None,
    mode: str | None,
    list_actions: bool,
    action_id: str | None,
    allow_high_risk: bool,
    json_mode: bool,
) -> None:
    """列出或安全执行 workflow next_actions（不会执行 suggested_command 字符串）。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        if list_actions and action_id:
            raise click.ClickException("不能同时使用 --list 和 --action。")
        project_info = _resolve_project(project)
        bundle = _load_next_context(project_info, mode=mode)
        source = _source_summary(bundle)
        actions = bundle.get("next_actions") or []
        if list_actions or not action_id:
            data = {
                "project": project_info["name"],
                "project_path": project_info["path"],
                "source": source,
                "next_actions": actions,
            }
            if json_mode:
                emit_json_payload("workflow next", ok=True, data=data)
                return
            click.echo(f"项目: {project_info['name']}")
            click.echo(f"来源: {source.get('type') or '-'} / {source.get('mode') or '-'}")
            if not actions:
                echo("[yellow]没有可用 next_actions[/yellow]")
                return
            for action in actions:
                click.echo(f"- {action['id']}\t{action.get('risk') or '-'}\t{action.get('label') or '-'}")
            return

        action = _find_next_action(bundle, action_id)
        result = _execute_next_action(project_info, bundle, action, allow_high_risk=allow_high_risk)
    except (click.ClickException, OSError, ValueError, json.JSONDecodeError) as exc:
        _emit_error(ctx, "workflow next", json_mode, exc)
        return

    data = {
        "project": project_info["name"],
        "project_path": project_info["path"],
        "source": source,
        "action": action,
        "result": result,
    }
    if json_mode:
        emit_json_payload("workflow next", ok=True, data=data)
        return

    echo(f"[green][OK] 已执行 workflow next：{action['id']}[/green]")
    if action["id"] == "plan_from_spec":
        click.echo(f"plan: {result.get('plan_path')}")
    elif action["id"] == "import_tasks":
        click.echo(f"导入任务: {result.get('count')}")


workflow_group.add_command(status_cmd)
workflow_group.add_command(next_cmd)
