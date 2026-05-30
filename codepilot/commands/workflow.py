"""Workflow mode state commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.config import load_project_config, resolve_project_config_reference
from codepilot.core.output import echo
from codepilot.core.workflow_state import (
    filter_consumed_workflow_next_actions,
    get_agent_session,
    mark_workflow_actions_consumed,
    normalize_workflow_next_actions,
    read_workflow_state,
    workflow_dirs,
    workflow_action_ids_consumed_by,
    workflow_payload_with_consumable_actions,
)
from codepilot.storage import database as db
from codepilot.core.supervisor import build_supervisor_analysis, execute_supervisor_auto


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


def _payload_context_paths(payload: dict[str, Any] | None) -> set[str]:
    if not payload:
        return set()
    paths: set[str] = set()
    raw_context = str(payload.get("context_path") or "").strip()
    if raw_context:
        paths.add(raw_context)
    artifacts = payload.get("artifact_paths") if isinstance(payload.get("artifact_paths"), dict) else {}
    artifact_context = str(artifacts.get("context") or "").strip()
    if artifact_context:
        paths.add(artifact_context)
    return paths


def _memory_consumed_records(project_info: dict[str, Any], payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    context_paths = _payload_context_paths(payload)
    if not context_paths:
        return []
    try:
        from codepilot.core.memory import read_memory_events
    except Exception:
        return []
    try:
        events = read_memory_events(project_info, event_type="workflow.action_executed", limit=500)
    except Exception:
        return []

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        details = dict(event.get("details") or {})
        action_id = str(details.get("action_id") or "").strip()
        if not action_id:
            continue
        event_source = details.get("source") if isinstance(details.get("source"), dict) else {}
        event_context = str(event_source.get("context_path") or "").strip()
        if event_context not in context_paths:
            continue
        consumed_at = str(event.get("timestamp") or "")
        for item_id in workflow_action_ids_consumed_by(action_id):
            key = (item_id, event_context)
            if key in seen:
                continue
            seen.add(key)
            record: dict[str, Any] = {
                "id": item_id,
                "consumed_at": consumed_at,
                "source": dict(event_source),
                "status": "consumed" if item_id == action_id else "expired",
            }
            if item_id != action_id:
                record["superseded_by"] = action_id
            records.append(record)
    return records


def _payload_with_memory_consumption(project_info: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    view = dict(payload)
    records = _memory_consumed_records(project_info, view)
    if records:
        view["consumed_actions"] = list(view.get("consumed_actions") or []) + records
    if isinstance(view.get("phase_history"), list):
        phase_history: list[Any] = []
        for entry in view.get("phase_history") or []:
            if not isinstance(entry, dict):
                phase_history.append(entry)
                continue
            entry_view = dict(entry)
            mode_state = entry_view.get("mode_state")
            if isinstance(mode_state, dict):
                entry_view["mode_state"] = _payload_with_memory_consumption(project_info, mode_state)
            phase_history.append(entry_view)
        view["phase_history"] = phase_history
    return view


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
    return normalize_workflow_next_actions(raw_actions)


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
        state = _payload_with_memory_consumption(project_info, state) or state
        context_path, context = _read_context(project_path, state.get("context_path"))
        next_actions = filter_consumed_workflow_next_actions(
            context.get("next_actions") or state.get("next_actions") or [],
            context,
            state,
        )
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
        agent_session = _payload_with_memory_consumption(project_info, agent_session) or agent_session
        artifacts = agent_session.get("artifact_paths") or {}
        context_path, context = _read_context(project_path, artifacts.get("context"))
        next_actions = filter_consumed_workflow_next_actions(
            context.get("next_actions")
            or agent_session.get("next_action_details")
            or agent_session.get("next_actions")
            or [],
            context,
            agent_session,
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


@dataclass(frozen=True)
class WorkflowAutoPolicy:
    allow_create_inspect_tasks: bool = False
    allow_import_plan_tasks: bool = False
    max_steps: int = 1
    failure_threshold: int = 1


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_workflow_auto_policy(project_info: dict[str, Any]) -> WorkflowAutoPolicy:
    cfg = load_project_config(project_info)
    automation = getattr(cfg, "automation", None) if cfg else None
    if automation is None:
        return WorkflowAutoPolicy()
    return WorkflowAutoPolicy(
        allow_create_inspect_tasks=bool(
            getattr(automation, "workflow_auto_create_inspect_tasks", False)
        ),
        allow_import_plan_tasks=bool(
            getattr(automation, "workflow_auto_import_plan_tasks", False)
        ),
        max_steps=_positive_int(getattr(automation, "workflow_auto_max_steps", 1), 1),
        failure_threshold=_positive_int(
            getattr(automation, "workflow_auto_failure_threshold", 1),
            1,
        ),
    )


def _workflow_auto_policy_payload(policy: WorkflowAutoPolicy) -> dict[str, Any]:
    return {
        "allow_create_inspect_tasks": policy.allow_create_inspect_tasks,
        "allow_import_plan_tasks": policy.allow_import_plan_tasks,
        "max_steps": policy.max_steps,
        "failure_threshold": policy.failure_threshold,
    }


def _risk_allows_policy_action(action: dict[str, Any]) -> bool:
    return str(action.get("risk") or "").lower() in {"low", "medium"}


def _select_auto_next_action(
    project_info: dict[str, Any],
    bundle: dict[str, Any],
    *,
    source: dict[str, Any],
    policy: WorkflowAutoPolicy,
    blocked_action_ids: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    actions = [dict(action) for action in bundle.get("next_actions") or []]
    blocked = set(blocked_action_ids or set())
    negative_report_ids = _negative_report_feedback_candidate_ids(project_info)
    for action in actions:
        action_id = str(action.get("id") or "")
        if action_id in blocked:
            continue
        if str(action.get("risk") or "").lower() != "low":
            continue
        if not action_id.startswith("ignore_inspect_report_"):
            continue
        if _workflow_action_was_executed(project_info, action_id=action_id, source=source):
            continue
        candidate_id = str(action.get("candidate_id") or "") or _candidate_id_from_report_action(
            action_id, "ignore_inspect_report_"
        )
        if candidate_id in negative_report_ids:
            return action, "negative_feedback_report_only"

    if policy.allow_create_inspect_tasks:
        for action in actions:
            action_id = str(action.get("id") or "")
            if action_id in blocked or action_id != "create_inspect_tasks":
                continue
            if not _risk_allows_policy_action(action):
                continue
            if _workflow_action_was_executed(project_info, action_id=action_id, source=source):
                continue
            return action, "policy_allowed_inspect_task_creation"

    if policy.allow_import_plan_tasks:
        for action in actions:
            action_id = str(action.get("id") or "")
            if action_id in blocked or action_id != "import_tasks":
                continue
            if not _risk_allows_policy_action(action):
                continue
            if _workflow_action_was_executed(project_info, action_id=action_id, source=source):
                continue
            return action, "policy_allowed_plan_import"

    for action in actions:
        action_id = str(action.get("id") or "")
        if action_id in blocked:
            continue
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
    spec_path = _resolve_project_file(project_path, _artifact_value(bundle, "spec"), label="spec")
    if spec_path is None:
        raise click.ClickException("当前 workflow context 没有可用于 plan_from_spec 的 spec。")
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


def _mark_workflow_action_consumed(
    project_info: dict[str, Any],
    *,
    action_id: str,
    source: dict[str, Any],
) -> None:
    mark_workflow_actions_consumed(
        project_info["path"],
        action_id=action_id,
        mode=str(source.get("mode") or "") or None,
        context_path=source.get("context_path") or None,
        source=source,
    )


def _status_context(project_path: Path, payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    raw_path = payload.get("context_path")
    if not raw_path:
        artifacts = payload.get("artifact_paths") if isinstance(payload.get("artifact_paths"), dict) else {}
        raw_path = artifacts.get("context")
    _, context = _read_context(project_path, raw_path)
    return context


def _status_payload_view(
    project_info: dict[str, Any],
    project_path: Path,
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    payload = _payload_with_memory_consumption(project_info, payload)
    return workflow_payload_with_consumable_actions(payload, _status_context(project_path, payload))


def workflow_status_payload(project: str | None = None, *, mode: str | None = None) -> dict[str, Any]:
    project_info = _resolve_project(project)
    project_path = Path(project_info["path"]).resolve()
    state = read_workflow_state(project_path, mode=mode) if mode else _latest_workflow_state(project_path)
    agent_session = get_agent_session(project_path)
    policy = resolve_workflow_auto_policy(project_info)
    return {
        "project": project_info["name"],
        "project_path": str(project_path),
        "mode": mode,
        "state": _status_payload_view(project_info, project_path, state),
        "agent_session": _status_payload_view(project_info, project_path, agent_session),
        "auto_policy": _workflow_auto_policy_payload(policy),
    }


def workflow_next_payload(project: str | None = None, *, mode: str | None = None) -> dict[str, Any]:
    project_info = _resolve_project(project)
    bundle = _load_next_context(project_info, mode=mode)
    policy = resolve_workflow_auto_policy(project_info)
    return {
        "project": project_info["name"],
        "project_path": project_info["path"],
        "source": _source_summary(bundle),
        "next_actions": bundle.get("next_actions") or [],
        "auto_policy": _workflow_auto_policy_payload(policy),
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
    _mark_workflow_action_consumed(project_info, action_id=action_id, source=source)
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
    mode: str | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    policy = resolve_workflow_auto_policy(project_info)
    policy_payload = _workflow_auto_policy_payload(policy)
    steps: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    blocked_action_ids: set[str] = set()
    stopped_reason = ""
    last_reason = "no_low_risk_auto_action"

    for _ in range(policy.max_steps):
        source = _source_summary(bundle)
        action, reason = _select_auto_next_action(
            project_info,
            bundle,
            source=source,
            policy=policy,
            blocked_action_ids=blocked_action_ids,
        )
        last_reason = reason
        if action is None:
            stopped_reason = reason
            break

        action_id = str(action.get("id") or "")
        try:
            result = _execute_next_action(project_info, bundle, action, allow_high_risk=allow_high_risk)
        except Exception as exc:
            failures.append({
                "action_id": action_id,
                "selected_reason": reason,
                "message": str(exc),
            })
            blocked_action_ids.add(action_id)
            if len(failures) >= policy.failure_threshold:
                raise click.ClickException(
                    f"自动推进失败达到熔断阈值 ({policy.failure_threshold})：{action_id}: {exc}"
                ) from exc
            continue

        _record_workflow_action_memory(
            project_info,
            action_id=action_id,
            action=action,
            source=source,
            result=result,
        )
        _mark_workflow_action_consumed(project_info, action_id=action_id, source=source)
        step = {
            "source": source,
            "selected_reason": reason,
            "action": action,
            "result": result,
        }
        steps.append(step)
        if len(steps) >= policy.max_steps:
            stopped_reason = "max_steps_reached"
            break

        try:
            bundle = _load_next_context(project_info, mode=mode)
        except click.ClickException:
            stopped_reason = "no_next_context"
            break

    source = _source_summary(bundle)
    if not steps:
        return {
            "project": project_info["name"],
            "project_path": project_info["path"],
            "source": source,
            "auto": True,
            "policy": policy_payload,
            "steps": [],
            "failures": failures,
            "action": None,
            "result": {},
            "skipped_reason": stopped_reason or last_reason,
        }

    last = steps[-1]
    return {
        "project": project_info["name"],
        "project_path": project_info["path"],
        "source": last["source"],
        "auto": True,
        "policy": policy_payload,
        "steps": steps,
        "failures": failures,
        "selected_reason": last["selected_reason"],
        "action": last["action"],
        "result": last["result"],
        "stopped_reason": stopped_reason or last_reason,
    }


def execute_workflow_auto_next_action(
    project: str | None,
    *,
    mode: str | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    project_info = _resolve_project(project)
    bundle = _load_next_context(project_info, mode=mode)
    return _execute_workflow_auto_next_action_payload(
        project_info,
        bundle,
        mode=mode,
        allow_high_risk=allow_high_risk,
    )


@click.command("status")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--mode", "-m", help="查看指定模式状态；不指定则查看最新 workflow")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def status_cmd(ctx: click.Context, project: str | None, mode: str | None, json_mode: bool) -> None:
    """查看当前 workflow 状态。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    project_path_obj = Path(project_info["path"]).resolve()
    project_path = str(project_path_obj)
    state = read_workflow_state(project_path, mode=mode) if mode else _latest_workflow_state(Path(project_path))
    agent_session = get_agent_session(project_path)
    policy = resolve_workflow_auto_policy(project_info)
    state_view = _status_payload_view(project_info, project_path_obj, state)
    agent_session_view = _status_payload_view(project_info, project_path_obj, agent_session)
    data = {
        "project": project_info["name"],
        "project_path": project_path,
        "mode": mode,
        "state": state_view,
        "agent_session": agent_session_view,
        "auto_policy": _workflow_auto_policy_payload(policy),
    }
    if json_mode:
        emit_json_payload("workflow status", ok=True, data=data)
        return

    if agent_session_view:
        click.echo("--- Agent Session ---")
        click.echo(f"session: {agent_session_view.get('session_id') or '-'}")
        click.echo(f"目标: {agent_session_view.get('goal') or '-'}")
        click.echo(f"当前阶段: {agent_session_view.get('current_phase') or '-'}")
        blocked = agent_session_view.get("blocked_reason")
        if blocked:
            click.echo(f"阻塞原因: {blocked}")
        actions = _format_next_actions(
            agent_session_view.get("next_action_details") or agent_session_view.get("next_actions")
        )
        if actions:
            click.echo(f"下一步: {', '.join(actions)}")
        click.echo()

    if state_view is None:
        target = f"模式 {mode}" if mode else "latest workflow"
        echo(f"[yellow]没有 {target} 状态[/yellow]")
        click.echo(f"项目: {project_info['name']}")
        click.echo(f"路径: {project_path}")
        return

    click.echo(f"项目: {project_info['name']}")
    click.echo(f"模式: {state_view.get('mode') or '-'}")
    click.echo(f"active: {state_view.get('active')}")
    click.echo(f"阶段: {state_view.get('current_phase') or '-'}")
    click.echo(f"session: {state_view.get('session_id') or '-'}")
    click.echo(f"context: {state_view.get('context_path') or '-'}")
    click.echo(f"updated_at: {state_view.get('updated_at') or '-'}")


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
                mode=mode,
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
        _mark_workflow_action_consumed(project_info, action_id=action_id, source=source)
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


@click.command("auto-policy")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def auto_policy_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """查看 workflow next --auto 自动推进策略配置。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    policy = resolve_workflow_auto_policy(project_info)
    payload = _workflow_auto_policy_payload(policy)

    if json_mode:
        emit_json_payload("workflow auto-policy", ok=True, data=payload)
        return

    click.echo(f"项目: {project_info['name']}")
    click.echo(f"allow_create_inspect_tasks: {str(payload['allow_create_inspect_tasks']).lower()}")
    click.echo(f"allow_import_plan_tasks:     {str(payload['allow_import_plan_tasks']).lower()}")
    click.echo(f"max_steps:                  {payload['max_steps']}")
    click.echo(f"failure_threshold:          {payload['failure_threshold']}")
    click.echo()
    click.echo("（通过 AGENTS.toml [automation] 配置：workflow_auto_create_inspect_tasks、")
    click.echo("  workflow_auto_import_plan_tasks、workflow_auto_max_steps、")
    click.echo("  workflow_auto_failure_threshold）")


@click.command("supervisor")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--task", "task_id", type=int, help="只分析指定任务")
@click.option("--limit", type=int, default=30, show_default=True, help="读取 trace 事件数量；0 表示不限制")
@click.option("--loop-threshold", type=int, default=2, show_default=True, help="同一任务同一动作连续执行达到阈值后转人工确认")
@click.option("--auto", "auto_action", is_flag=True, help="执行第一个 allowlist 低风险 Supervisor 建议")
@click.option("--action", "suggestion_id", help="执行指定 Supervisor suggestion id（仅支持 allowlist）")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def supervisor_cmd(
    ctx: click.Context,
    project: str | None,
    task_id: int | None,
    limit: int,
    loop_threshold: int,
    auto_action: bool,
    suggestion_id: str | None,
    json_mode: bool,
) -> None:
    """只读分析任务流，并可执行受控 allowlist 导向动作。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        if auto_action and suggestion_id:
            raise click.ClickException("不能同时使用 --auto 和 --action。")
        project_info = _resolve_project(project)
        if auto_action or suggestion_id:
            data = execute_supervisor_auto(
                project_info,
                task_id=task_id,
                limit=max(limit, 0),
                loop_threshold=max(loop_threshold, 1),
                suggestion_id=suggestion_id,
            )
        else:
            data = build_supervisor_analysis(
                project_info,
                task_id=task_id,
                limit=max(limit, 0),
                loop_threshold=max(loop_threshold, 1),
            )
    except (click.ClickException, OSError, ValueError, json.JSONDecodeError) as exc:
        _emit_error(ctx, "workflow supervisor", json_mode, exc)
        return

    if json_mode:
        emit_json_payload("workflow supervisor", ok=True, data=data)
        return

    click.echo(f"项目: {data.get('project')}")
    click.echo(f"Supervisor dry-run: {str(data.get('dry_run')).lower()}")
    suggestions = data.get("suggestions") or []
    if not suggestions:
        echo("[green]未发现需要 Supervisor 导向的任务流风险[/green]")
    else:
        for item in suggestions:
            executable = "auto" if item.get("auto_executable") else "manual"
            click.echo(
                f"- {item.get('id')}\t{item.get('severity')}\t{item.get('action')}\t"
                f"#{item.get('task_id')}\t{executable}\t{item.get('reason')}"
            )
    executed = data.get("executed")
    if executed:
        echo(f"[green][OK] Supervisor 已执行：{executed.get('action')} #{executed.get('task_id')}[/green]")
    elif data.get("dry_run") is False:
        echo(f"[yellow]Supervisor 未执行动作：{data.get('skipped_reason') or 'no_action'}[/yellow]")


workflow_group.add_command(status_cmd)
workflow_group.add_command(next_cmd)
workflow_group.add_command(auto_policy_cmd)
workflow_group.add_command(supervisor_cmd)
