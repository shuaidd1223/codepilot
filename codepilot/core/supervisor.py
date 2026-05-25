"""Deterministic Supervisor analysis and controlled workflow guidance."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from codepilot.commands.trace import collect_trace_events
from codepilot.core.memory import append_memory_event, read_memory_events
from codepilot.core.workflow_state import (
    append_task_timeline_event,
    get_agent_session,
    read_task_execution_artifacts,
    read_workflow_state,
    update_workflow_state,
    workflow_dirs,
)
from codepilot.storage import database as db


SUPERVISOR_ALLOWED_ACTIONS = frozenset({"mark_blocked", "request_clarification", "retry_with_hint"})
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|APP[_-]?SECRET|PRIVATE[_-]?KEY)[A-Z0-9_.-]*)"
    r"\s*([:=])\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/\-]+=*", re.IGNORECASE)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")


def _compact(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    text = _SECRET_ASSIGNMENT_RE.sub("credential=[redacted]", text)
    text = _BEARER_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _SK_RE.sub("[redacted]", text)
    return text[:limit]


def _append_text(base: str, note: str) -> str:
    base = str(base or "").rstrip()
    note = str(note or "").strip()
    if not base:
        return note
    if not note:
        return base
    return f"{base}\n\n{note}"


def _status_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _iter_mode_states(project_path: Path) -> list[dict[str, Any]]:
    state_dir = workflow_dirs(project_path)["state"]
    if not state_dir.is_dir():
        return []
    states: list[dict[str, Any]] = []
    for path in sorted(state_dir.glob("*-state.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["_state_path"] = str(path)
            states.append(payload)
    return states


def _latest_workflow_state(project_path: Path) -> dict[str, Any] | None:
    active = read_workflow_state(project_path)
    if active:
        return dict(active)
    states = _iter_mode_states(project_path)
    if not states:
        return None
    return dict(max(states, key=lambda item: (str(item.get("updated_at") or item.get("started_at") or ""), str(item.get("_state_path") or ""))))


def _workflow_state_view(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    return {
        "mode": str(state.get("mode") or ""),
        "active": bool(state.get("active")) if "active" in state else None,
        "current_phase": str(state.get("current_phase") or ""),
        "session_id": str(state.get("session_id") or ""),
        "context_path": str(state.get("context_path") or ""),
        "updated_at": str(state.get("updated_at") or ""),
        "next_action_count": len(state.get("next_actions") or []),
    }


def _agent_session_view(session: dict[str, Any] | None) -> dict[str, Any] | None:
    if not session:
        return None
    return {
        "session_id": str(session.get("session_id") or ""),
        "goal": _compact(session.get("goal"), limit=240),
        "current_phase": str(session.get("current_phase") or ""),
        "blocked_reason": _compact(session.get("blocked_reason"), limit=300),
        "linked_task_ids": list(session.get("linked_task_ids") or []),
        "next_action_count": len(session.get("next_action_details") or session.get("next_actions") or []),
        "updated_at": str(session.get("updated_at") or ""),
    }


def _artifact_status(item: dict[str, Any] | None, *keys: str) -> str:
    if not isinstance(item, dict):
        return ""
    for key in keys:
        value = _compact(item.get(key), limit=120)
        if value:
            return value
    return ""


def _task_artifact_summary(project_path: Path, task: dict[str, Any]) -> dict[str, Any]:
    task_id = int(task["id"])
    payload = read_task_execution_artifacts(project_path, task_id)
    if payload is None:
        return {
            "task_id": task_id,
            "artifact_path": "",
            "status": "",
            "validation_status": "",
            "review_status": "",
            "review_verdict": "",
            "artifact_kinds": [],
            "last_timeline_event": {},
            "timeline_count": 0,
        }
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    validation = artifacts.get("validation") if isinstance(artifacts.get("validation"), dict) else {}
    review = artifacts.get("review") if isinstance(artifacts.get("review"), dict) else {}
    timeline = payload.get("timeline") if isinstance(payload.get("timeline"), list) else []
    return {
        "task_id": task_id,
        "artifact_path": str(payload.get("artifact_path") or ""),
        "status": str(payload.get("status") or ""),
        "validation_status": _artifact_status(validation, "status", "verdict"),
        "review_status": _artifact_status(review, "status"),
        "review_verdict": _artifact_status(review, "verdict", "status"),
        "review_summary": _compact(review.get("summary") if isinstance(review, dict) else "", limit=240),
        "validation_summary": _compact(validation.get("summary") if isinstance(validation, dict) else "", limit=240),
        "artifact_kinds": sorted(str(key) for key in artifacts),
        "last_timeline_event": dict(timeline[-1]) if timeline else {},
        "timeline_count": len(timeline),
    }


def _source_event(artifact: dict[str, Any], task: dict[str, Any]) -> str:
    last = artifact.get("last_timeline_event") if isinstance(artifact.get("last_timeline_event"), dict) else {}
    event = str(last.get("event") or "").strip()
    if event:
        return f"task_timeline.{event}"
    return f"task.{task.get('status') or 'unknown'}"


def _failure_reason(task: dict[str, Any], artifact: dict[str, Any]) -> str:
    for key in ("review_summary", "validation_summary"):
        value = _compact(artifact.get(key), limit=300)
        if value:
            return value
    error = _compact(task.get("error_message"), limit=300)
    if error:
        return error
    last = artifact.get("last_timeline_event") if isinstance(artifact.get("last_timeline_event"), dict) else {}
    message = _compact(last.get("message"), limit=300)
    return message or "任务需要 Supervisor 复核。"


def _recent_action_count(project_info: dict[str, Any], *, task_id: int, action: str) -> int:
    count = 0
    for event in read_memory_events(project_info, event_type="supervisor.action_executed", limit=100):
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        try:
            event_task_id = int(details.get("task_id") or 0)
        except (TypeError, ValueError):
            event_task_id = 0
        event_action = str(details.get("action") or "")
        if event_task_id != int(task_id):
            continue
        if event_action == action:
            count += 1
            continue
        break
    return count


def _base_suggestion(
    task: dict[str, Any],
    artifact: dict[str, Any],
    *,
    action: str,
    severity: str,
    risk: str,
    reason: str,
    label: str,
) -> dict[str, Any]:
    task_id = int(task["id"])
    return {
        "id": f"task-{task_id}-{action}",
        "actor": "supervisor",
        "action": action,
        "task_id": task_id,
        "task_title": _compact(task.get("title"), limit=160),
        "severity": severity,
        "risk": risk,
        "label": label,
        "reason": _compact(reason, limit=400),
        "source_event": _source_event(artifact, task),
        "artifact_path": str(artifact.get("artifact_path") or ""),
        "auto_executable": action in SUPERVISOR_ALLOWED_ACTIONS and risk == "low",
    }


def _suggest_for_task(
    project_info: dict[str, Any],
    task: dict[str, Any],
    artifact: dict[str, Any],
    *,
    loop_threshold: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    status = str(task.get("status") or "")
    task_id = int(task["id"])
    retry_count = int(task.get("retry_count") or 0)
    max_retries = int(task.get("max_retries") or 3)
    reason = _failure_reason(task, artifact)
    downgraded: list[dict[str, Any]] = []

    suggestion: dict[str, Any] | None = None
    if status == "failed":
        if retry_count < max_retries:
            suggestion = _base_suggestion(
                task,
                artifact,
                action="retry_with_hint",
                severity="medium",
                risk="low",
                reason=reason,
                label="追加失败证据后重试同一任务",
            )
        else:
            suggestion = _base_suggestion(
                task,
                artifact,
                action="request_clarification",
                severity="high",
                risk="low",
                reason=f"任务已达到重试上限：{reason}",
                label="请求人工确认失败处理方向",
            )
    elif status == "backlog" and _compact(task.get("error_message"), limit=1):
        suggestion = _base_suggestion(
            task,
            artifact,
            action="mark_blocked",
            severity="medium",
            risk="low",
            reason=reason,
            label="记录阻塞原因并阻止无证据重复执行",
        )
    elif status == "cancelled":
        suggestion = _base_suggestion(
            task,
            artifact,
            action="request_clarification",
            severity="medium",
            risk="low",
            reason=reason,
            label="请求人工确认是否恢复任务",
        )
    elif status == "done" and "patch" in (artifact.get("artifact_kinds") or []) and not artifact.get("review_verdict"):
        suggestion = _base_suggestion(
            task,
            artifact,
            action="promote_to_review",
            severity="medium",
            risk="high",
            reason="任务已有 patch 摘要但缺少 review verdict。",
            label="建议转入人工 Review",
        )
        suggestion["auto_executable"] = False

    if suggestion and suggestion["action"] in SUPERVISOR_ALLOWED_ACTIONS:
        repeated = _recent_action_count(project_info, task_id=task_id, action=str(suggestion["action"]))
        suggestion["recent_same_action_count"] = repeated
        if repeated >= max(loop_threshold, 1) and suggestion["action"] != "request_clarification":
            original = str(suggestion["action"])
            suggestion = dict(suggestion)
            suggestion["id"] = f"task-{task_id}-request_clarification"
            suggestion["action"] = "request_clarification"
            suggestion["label"] = "连续相同动作达到阈值，转人工确认"
            suggestion["reason"] = f"连续 {repeated} 次建议 {original} 后仍未收敛：{suggestion['reason']}"
            suggestion["downgraded_from"] = original
            suggestion["auto_executable"] = True
            downgraded.append(
                {
                    "task_id": task_id,
                    "from_action": original,
                    "to_action": "request_clarification",
                    "recent_same_action_count": repeated,
                    "threshold": max(loop_threshold, 1),
                }
            )

    return suggestion, downgraded


def build_supervisor_analysis(
    project_info: dict[str, Any],
    *,
    task_id: int | None = None,
    limit: int = 30,
    loop_threshold: int = 2,
) -> dict[str, Any]:
    """Build a read-only Supervisor analysis from existing workflow facts."""
    project_name = str(project_info.get("name") or "")
    project_path = Path(str(project_info.get("path") or "")).expanduser().resolve()
    tasks = db.list_tasks(project=project_name)
    if task_id is not None:
        tasks = [task for task in tasks if int(task.get("id") or 0) == int(task_id)]
    tasks = sorted(tasks, key=lambda item: int(item.get("id") or 0))
    artifacts = [_task_artifact_summary(project_path, task) for task in tasks]
    artifacts_by_task = {int(item["task_id"]): item for item in artifacts}
    trace_events = collect_trace_events(project_info, task_id=task_id, limit=max(limit, 0))

    suggestions: list[dict[str, Any]] = []
    risks: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    downgraded: list[dict[str, Any]] = []
    for task in tasks:
        artifact = artifacts_by_task[int(task["id"])]
        suggestion, task_downgraded = _suggest_for_task(
            project_info,
            task,
            artifact,
            loop_threshold=loop_threshold,
        )
        downgraded.extend(task_downgraded)
        if not suggestion:
            continue
        suggestions.append(suggestion)
        record = {
            "task_id": suggestion["task_id"],
            "severity": suggestion["severity"],
            "summary": suggestion["reason"],
            "source_event": suggestion["source_event"],
            "artifact_path": suggestion["artifact_path"],
        }
        if suggestion["action"] in {"mark_blocked", "request_clarification"}:
            blockers.append(record)
        else:
            risks.append(record)

    suggestions.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2}.get(str(item.get("severity")), 9), int(item.get("task_id") or 0), str(item.get("action") or "")))
    workflow_state = _latest_workflow_state(project_path)
    agent_session = get_agent_session(project_path)
    return {
        "mode": "supervisor",
        "dry_run": True,
        "project": project_name,
        "project_path": str(project_path),
        "observed": {
            "tasks": {
                "total": len(tasks),
                "by_status": _status_counts(tasks),
                "focus": [
                    {
                        "id": int(task["id"]),
                        "title": _compact(task.get("title"), limit=160),
                        "status": str(task.get("status") or ""),
                        "run_phase": str(task.get("run_phase") or ""),
                        "retry_count": int(task.get("retry_count") or 0),
                        "max_retries": int(task.get("max_retries") or 3),
                    }
                    for task in tasks
                ],
            },
            "workflow_state": _workflow_state_view(workflow_state),
            "agent_session": _agent_session_view(agent_session),
            "trace": {"count": len(trace_events), "events": trace_events},
            "artifacts": artifacts,
        },
        "analysis": {
            "risks": risks,
            "deviations": [],
            "blockers": blockers,
        },
        "suggestions": suggestions,
        "allowlist": sorted(SUPERVISOR_ALLOWED_ACTIONS),
        "loop_control": {
            "threshold": max(loop_threshold, 1),
            "downgraded": downgraded,
        },
    }


def _find_suggestion(payload: dict[str, Any], suggestion_id: str) -> dict[str, Any]:
    wanted = str(suggestion_id or "").strip()
    for suggestion in payload.get("suggestions") or []:
        if str(suggestion.get("id") or "") == wanted:
            return dict(suggestion)
    raise ValueError(f"未找到 supervisor suggestion：{wanted}")


def _first_auto_suggestion(payload: dict[str, Any]) -> dict[str, Any] | None:
    for suggestion in payload.get("suggestions") or []:
        if suggestion.get("auto_executable") and str(suggestion.get("action") or "") in SUPERVISOR_ALLOWED_ACTIONS:
            return dict(suggestion)
    return None


def _retry_hint_note(suggestion: dict[str, Any]) -> str:
    return "\n".join(
        [
            "## Supervisor 重试提示",
            "",
            f"- 来源事件: {suggestion.get('source_event') or '-'}",
            f"- 失败摘要: {_compact(suggestion.get('reason'), limit=320) or '（无）'}",
            "- 导向: 保持原任务目标，优先修复上述失败点，再运行原任务的相关验证。",
        ]
    )


def _record_supervisor_action(
    project_info: dict[str, Any],
    project_path: Path,
    task: dict[str, Any],
    suggestion: dict[str, Any],
    updated: dict[str, Any] | None,
) -> dict[str, Any]:
    action = str(suggestion.get("action") or "")
    reason = _compact(suggestion.get("reason"), limit=400)
    source_event = _compact(suggestion.get("source_event"), limit=120)
    artifact_path = _compact(suggestion.get("artifact_path"), limit=500)
    task_id = int(task["id"])
    append_task_timeline_event(
        project_path,
        task_id,
        event="supervised",
        actor="supervisor",
        source="codepilot.supervisor",
        message=f"{action}: {reason}; source_event={source_event}",
        artifact_path=artifact_path or None,
    )
    details = {
        "actor": "supervisor",
        "action": action,
        "suggestion_id": str(suggestion.get("id") or ""),
        "task_id": task_id,
        "reason": reason,
        "source_event": source_event,
        "artifact_path": artifact_path,
        "status_before": str(task.get("status") or ""),
        "status_after": str((updated or {}).get("status") or task.get("status") or ""),
    }
    try:
        append_memory_event(
            project_info,
            event_type="supervisor.action_executed",
            source="codepilot.supervisor",
            summary=f"Supervisor 执行 {action}：#{task_id}",
            details=details,
            tags=["supervisor", "control"],
        )
    except Exception:
        pass
    try:
        update_workflow_state(
            project_path,
            "supervisor",
            active=False,
            current_phase="action_executed",
            last_action=details,
            next_actions=[],
        )
    except Exception:
        pass
    return details


def execute_supervisor_suggestion(
    project_info: dict[str, Any],
    suggestion: dict[str, Any],
) -> dict[str, Any]:
    action = str(suggestion.get("action") or "").strip()
    if action not in SUPERVISOR_ALLOWED_ACTIONS:
        raise ValueError(f"Supervisor 不允许执行动作：{action}")
    task_id = int(suggestion.get("task_id") or 0)
    task = db.get_task(task_id)
    if not task:
        raise ValueError(f"任务 #{task_id} 不存在")
    status = str(task.get("status") or "")
    if status == "in_progress":
        raise ValueError(f"任务 #{task_id} 正在运行，Supervisor 不直接改动运行中任务")

    project_path = Path(str(project_info.get("path") or "")).expanduser().resolve()
    updated: dict[str, Any] | None
    if action == "retry_with_hint":
        if status in {"done", "archived"}:
            raise ValueError(f"任务 #{task_id} 状态为 {status}，不能自动重试")
        reset = db.reset_task_for_retry(task_id, reset_retry_count=False)
        updated = db.update_task(task_id, content=_append_text(reset.get("content") or "", _retry_hint_note(suggestion)))
    elif action == "mark_blocked":
        updated = db.update_task(
            task_id,
            status="failed",
            error_message=f"Supervisor mark_blocked: {_compact(suggestion.get('reason'), limit=380)}",
            run_phase=None,
            heartbeat_at=None,
            active_pid=None,
            current_log_path=None,
            last_output=None,
            stop_requested=0,
            stop_reason=None,
        )
    else:
        updated = db.update_task(
            task_id,
            status="failed" if status not in {"failed", "cancelled"} else status,
            error_message=f"Supervisor request_clarification: {_compact(suggestion.get('reason'), limit=360)}",
            run_phase=None,
            heartbeat_at=None,
            active_pid=None,
            current_log_path=None,
            last_output=None,
            stop_requested=0,
            stop_reason=None,
        )

    audit = _record_supervisor_action(project_info, project_path, task, suggestion, updated)
    return {
        "action": action,
        "suggestion_id": str(suggestion.get("id") or ""),
        "task_id": task_id,
        "status_before": status,
        "status_after": str((updated or {}).get("status") or ""),
        "audit": audit,
    }


def execute_supervisor_auto(
    project_info: dict[str, Any],
    *,
    task_id: int | None = None,
    limit: int = 30,
    loop_threshold: int = 2,
    suggestion_id: str | None = None,
) -> dict[str, Any]:
    payload = build_supervisor_analysis(
        project_info,
        task_id=task_id,
        limit=limit,
        loop_threshold=loop_threshold,
    )
    suggestion = _find_suggestion(payload, suggestion_id) if suggestion_id else _first_auto_suggestion(payload)
    payload = dict(payload)
    payload["dry_run"] = False
    if suggestion is None:
        payload["executed"] = None
        payload["skipped_reason"] = "no_allowlisted_supervisor_action"
        return payload
    payload["executed"] = execute_supervisor_suggestion(project_info, suggestion)
    return payload
