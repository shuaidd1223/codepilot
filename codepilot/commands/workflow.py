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


def _candidate_id_from_report_action(action_id: str, prefix: str) -> str:
    return action_id[len(prefix) :] if action_id.startswith(prefix) else ""


def _negative_report_feedback_candidate_ids(project_info: dict[str, Any]) -> set[str]:
    try:
        from codepilot.core.memory import read_memory_candidates
    except Exception:
        return set()

    candidate_ids: set[str] = set()
    try:
        candidates = read_memory_candidates(project_info, limit=500)
    except Exception:
        return candidate_ids
    for candidate in candidates:
        if str(candidate.get("feedback") or "") != "negative":
            continue
        try:
            score = int(candidate.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score > 30:
            continue
        details = dict(candidate.get("details") or {})
        raw_candidate_id = str(details.get("candidate_id") or "").strip()
        if raw_candidate_id:
            candidate_ids.add(raw_candidate_id)
        action_id = str(details.get("action_id") or "").strip()
        for prefix in ("ignore_inspect_report_", "delete_inspect_report_", "archive_inspect_report_"):
            from_action = _candidate_id_from_report_action(action_id, prefix)
            if from_action:
                candidate_ids.add(from_action)
    return candidate_ids


def _workflow_action_was_executed(project_info: dict[str, Any], *, action_id: str, source: dict[str, Any]) -> bool:
    try:
        from codepilot.core.memory import read_memory_events
    except Exception:
        return False

    source_context = str(source.get("context_path") or "")
    try:
        events = read_memory_events(project_info, event_type="workflow.action_executed", limit=500)
    except Exception:
        return False
    for event in events:
        details = dict(event.get("details") or {})
        if str(details.get("action_id") or "") != action_id:
            continue
        event_source = details.get("source") if isinstance(details.get("source"), dict) else {}
        event_context = str(event_source.get("context_path") or "")
        if source_context and event_context and event_context != source_context:
            continue
        return True
    return False


def _select_auto_next_action(
    project_info: dict[str, Any],
    bundle: dict[str, Any],
    *,
    source: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    actions = [dict(action) for action in bundle.get("next_actions") or []]
    negative_report_ids = _negative_report_feedback_candidate_ids(project_info)
    for action in actions:
        action_id = str(action.get("id") or "")
        if str(action.get("risk") or "").lower() != "low":
            continue
        if not action_id.startswith("ignore_inspect_report_"):
            continue
        candidate_id = str(action.get("candidate_id") or "") or _candidate_id_from_report_action(
            action_id, "ignore_inspect_report_"
        )
        if candidate_id in negative_report_ids:
            return action, "negative_feedback_report_only"

    for action in actions:
        action_id = str(action.get("id") or "")
        if action_id != "plan_from_inspect":
            continue
        if str(action.get("risk") or "").lower() != "low":
            continue
        if _workflow_action_was_executed(project_info, action_id=action_id, source=source):
            continue
        return action, "low_risk_inspect_plan"

    return None, "no_low_risk_auto_action"


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


def _execute_inspect_workflow_action(project_info: dict, bundle: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    from codepilot.commands.inspect_workflow import execute_inspect_workflow_action

    context_path = bundle.get("context_path") or (bundle.get("state") or {}).get("context_path") or ""
    return execute_inspect_workflow_action(
        project_info,
        action_id=str(action.get("id") or ""),
        context_path=context_path,
    )


_ALLOWED_NEXT_ACTIONS = {
    "plan_from_spec": _execute_plan_from_spec,
    "import_tasks": _execute_import_tasks,
}


def _is_inspect_next_action(action_id: str) -> bool:
    return (
        action_id in {"create_inspect_tasks", "plan_from_inspect"}
        or action_id.startswith("promote_inspect_report_")
        or action_id.startswith("ignore_inspect_report_")
        or action_id.startswith("delete_inspect_report_")
        or action_id.startswith("archive_inspect_report_")
    )


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
        if _is_inspect_next_action(action_id):
            return _execute_inspect_workflow_action(project_info, bundle, action)
        allowed = ", ".join(sorted(_ALLOWED_NEXT_ACTIONS))
        inspect_allowed = (
            "create_inspect_tasks, plan_from_inspect, promote_inspect_report_<candidate_id>, "
            "ignore_inspect_report_<candidate_id>, delete_inspect_report_<candidate_id>, "
            "archive_inspect_report_<candidate_id>"
        )
        raise click.ClickException(f"不支持的 next_action：{action_id}。当前 allowlist：{allowed}, {inspect_allowed}。")
    return handler(project_info, bundle)


def _record_workflow_action_memory(
    project_info: dict[str, Any],
    *,
    action_id: str,
    action: dict[str, Any],
    source: dict[str, Any],
    result: Any,
) -> None:
    try:
        from codepilot.core.memory import append_memory_event

        append_memory_event(
            project_info,
            event_type="workflow.action_executed",
            source="codepilot.workflow",
            summary=f"执行 workflow action：{action_id}",
            details={
                "action_id": action_id,
                "risk": action.get("risk") or "",
                "source": source,
                "result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
            },
            tags=["workflow", "action"],
        )
    except Exception:
        return


def workflow_status_payload(project: str | None = None, *, mode: str | None = None) -> dict[str, Any]:
    project_info = _resolve_project(project)
    project_path = str(project_info["path"])
    state = read_workflow_state(project_path, mode=mode) if mode else _latest_workflow_state(Path(project_path))
    agent_session = get_agent_session(project_path)
    return {
        "project": project_info["name"],
        "project_path": project_path,
        "mode": mode,
        "state": state,
        "agent_session": agent_session,
    }


def workflow_next_payload(project: str | None = None, *, mode: str | None = None) -> dict[str, Any]:
    project_info = _resolve_project(project)
    bundle = _load_next_context(project_info, mode=mode)
    return {
        "project": project_info["name"],
        "project_path": project_info["path"],
        "source": _source_summary(bundle),
        "next_actions": bundle.get("next_actions") or [],
    }


def execute_workflow_next_action(
    project: str | None,
    action_id: str,
    *,
    mode: str | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    project_info = _resolve_project(project)
    bundle = _load_next_context(project_info, mode=mode)
    action = _find_next_action(bundle, action_id)
    result = _execute_next_action(project_info, bundle, action, allow_high_risk=allow_high_risk)
    source = _source_summary(bundle)
    _record_workflow_action_memory(
        project_info,
        action_id=action_id,
        action=action,
        source=source,
        result=result,
    )
    payload = {
        "project": project_info["name"],
        "project_path": project_info["path"],
        "source": source,
        "action": action,
        "result": result,
    }
    return payload


def _execute_workflow_auto_next_action_payload(
    project_info: dict[str, Any],
    bundle: dict[str, Any],
    *,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    source = _source_summary(bundle)
    action, reason = _select_auto_next_action(project_info, bundle, source=source)
    if action is None:
        return {
            "project": project_info["name"],
            "project_path": project_info["path"],
            "source": source,
            "auto": True,
            "action": None,
            "result": {},
            "skipped_reason": reason,
        }

    action_id = str(action.get("id") or "")
    result = _execute_next_action(project_info, bundle, action, allow_high_risk=allow_high_risk)
    _record_workflow_action_memory(
        project_info,
        action_id=action_id,
        action=action,
        source=source,
        result=result,
    )
    return {
        "project": project_info["name"],
        "project_path": project_info["path"],
        "source": source,
        "auto": True,
        "selected_reason": reason,
        "action": action,
        "result": result,
    }


def execute_workflow_auto_next_action(
    project: str | None,
    *,
    mode: str | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    project_info = _resolve_project(project)
    bundle = _load_next_context(project_info, mode=mode)
    return _execute_workflow_auto_next_action_payload(project_info, bundle, allow_high_risk=allow_high_risk)


@click.command("status")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--mode", "-m", help="查看指定模式状态；不指定则查看最新 workflow")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def status_cmd(ctx: click.Context, project: str | None, mode: str | None, json_mode: bool) -> None:
    """查看当前 workflow 状态。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    project_path = str(project_info["path"])
    state = read_workflow_state(project_path, mode=mode) if mode else _latest_workflow_state(Path(project_path))
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
        target = f"模式 {mode}" if mode else "latest workflow"
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
@click.option("--auto", "auto_action", is_flag=True, help="自动执行一个低风险 next_action；不会执行中高风险动作")
@click.option("--allow-high-risk", is_flag=True, help="显式允许高风险动作通过风险检查；动作仍必须在 allowlist 内")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def next_cmd(
    ctx: click.Context,
    project: str | None,
    mode: str | None,
    list_actions: bool,
    action_id: str | None,
    auto_action: bool,
    allow_high_risk: bool,
    json_mode: bool,
) -> None:
    """列出或安全执行 workflow next_actions（不会执行 suggested_command 字符串）。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        if list_actions and action_id:
            raise click.ClickException("不能同时使用 --list 和 --action。")
        if auto_action and (list_actions or action_id):
            raise click.ClickException("不能同时使用 --auto 与 --list/--action。")
        project_info = _resolve_project(project)
        bundle = _load_next_context(project_info, mode=mode)
        source = _source_summary(bundle)
        actions = bundle.get("next_actions") or []
        if auto_action:
            data = _execute_workflow_auto_next_action_payload(
                project_info,
                bundle,
                allow_high_risk=allow_high_risk,
            )
            if json_mode:
                emit_json_payload("workflow next", ok=True, data=data)
                return
            if data.get("action") is None:
                echo(f"[yellow]没有可自动执行的低风险 next_action：{data.get('skipped_reason')}[/yellow]")
                return
            echo(f"[green][OK] 自动执行 workflow next：{data['action']['id']}[/green]")
            return
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
        _record_workflow_action_memory(
            project_info,
            action_id=action_id,
            action=action,
            source=source,
            result=result,
        )
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
