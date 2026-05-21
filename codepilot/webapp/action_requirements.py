"""Requirement and task-dispatch actions for the Web UI."""

from __future__ import annotations

import json
import re
import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from codepilot.storage import database as db
from codepilot.ai_support.clarification_protocol import (
    build_clarification_input_summary,
    normalize_clarification_history,
    normalize_clarification_questions,
    normalize_text,
    render_clarification_questions,
)
from codepilot.commands.auto import normalize_requirement_text
from codepilot.ai_support.interaction_controller import (
    ClarificationTransition,
    build_workflow_session_record,
    interpret_clarification_outcome,
    parse_intent_prefix,
    resolve_turn_intent,
)
from codepilot.webapp.action_state import (
    _GOAL_MAX_BYTES,
    _MAX_JOB_LOG_LINES,
    _append_event,
    _get_job,
    _emit_ui_state_event,
    _effective_planner,
    _extract_job_task_ids,
    _is_job_cancel_requested,
    _next_job_id,
    _normalize_goal_category,
    _shell,
    _update_job,
    cancel_ui_job,
)
from codepilot.webapp.payloads import _now_iso, _task_payload


_PLANNER_PID_RE = re.compile(r"\bPID=(\d+)\b")


def _actions():
    module = sys.modules.get("codepilot.webapp.actions")
    if module is None:
        from codepilot.webapp import actions as module
    return module


def _format_numbered_questions(questions: list[dict]) -> str:
    return render_clarification_questions(questions)


def _answer_project_question(project_info: dict, question: str, *, gateway_options=None, history: Optional[list[dict]] = None) -> str:
    from codepilot.ai_support.service import answer_question_via_api

    shared_gateway_options = gateway_options or _actions().resolve_shared_gateway_options(project_info)
    try:
        answer = answer_question_via_api(
            provider_key=shared_gateway_options.classifier_provider,
            question=question,
            gateway_options=shared_gateway_options,
            history=history,
        )
    except Exception as exc:
        answer = f"回答失败：{exc}"
    return answer or "未获得回答"


def _submit_requirement_from_message(
    project: str,
    refined_title: str,
    *,
    planner: str,
    max_tasks: int,
) -> tuple[dict, str, list[int]]:
    result = _actions().submit_requirement_action(
        project,
        refined_title,
        execute=True,
        planner=planner,
        max_tasks=max_tasks,
        run_async=True,
        clarify=False,
    )
    reply = result.get("message") or "需求已提交"
    return result, reply, _extract_job_task_ids(result)


def _request_task_service_start(project: str) -> tuple[dict | None, str]:
    try:
        from codepilot.commands.daemon import request_daemon_service_start

        return request_daemon_service_start(project), ""
    except Exception as exc:
        return None, str(exc)


def _append_requirement_job_log(job_id: int, line: str, *, project: str | None = None) -> None:
    shell = _shell()
    payload = {"id": int(job_id), "line": line, "updated_at": _now_iso()}
    persisted = _get_job(int(job_id))
    with shell._UI_LOCK:
        job = shell._UI_JOBS.get(int(job_id))
        if job is None:
            job = dict(persisted or {"id": int(job_id)})
            shell._UI_JOBS[int(job_id)] = job
        job_log = job.setdefault("log", [])
        if isinstance(job_log, list):
            job_log.append(line)
            if len(job_log) > _MAX_JOB_LOG_LINES:
                del job_log[:-_MAX_JOB_LOG_LINES]
        planner_pids = job.setdefault("planner_pids", [])
        if isinstance(planner_pids, list) and "规划进程已启动" in line:
            for match in _PLANNER_PID_RE.finditer(line):
                try:
                    pid = int(match.group(1))
                except Exception:
                    continue
                if pid > 0 and pid not in planner_pids:
                    planner_pids.append(pid)
        job["updated_at"] = _now_iso()
        payload["updated_at"] = job["updated_at"]
        snapshot = dict(job)
    _update_job(int(job_id), **snapshot)
    _emit_ui_state_event("job_log", project=project or str(snapshot.get("project") or ""), payload=payload)


def _start_requirement_job_process(job_id: int, project_info: dict) -> subprocess.Popen:
    from codepilot.core.runtime import codepilot_command, no_window_kwargs

    project_path = str(project_info.get("path") or "").strip()
    cwd = project_path if project_path and Path(project_path).exists() else None
    cmd = codepilot_command("requirement-worker", str(int(job_id)), module="codepilot.cli")
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **no_window_kwargs(new_process_group=True),
    )


@dataclass(frozen=True)
class _GoalDispatchContext:
    project: str
    project_info: dict
    planner: str
    text: str
    clarify_answers: Optional[list[dict]]
    clarify_questions: Optional[list[dict]]
    category: str
    qa_history: Optional[list[dict]]
    original_title: str
    gateway_options: object
    forced_intent: Optional[str]


@dataclass(frozen=True)
class _RequirementJobContext:
    job_id: int
    job: dict
    request: dict
    project: str
    title: str
    project_info: dict
    execute: bool
    planner: str
    agent: object
    priority: str
    max_tasks: int
    executor: str
    auto_commit: bool
    max_retries: int
    task_source: str


def _dispatch_with_intent_handlers(
    intent: str,
    *,
    handlers: dict[str, Callable[[], dict]],
    fallback: Callable[[], dict],
) -> dict:
    handler = handlers.get(intent)
    if handler is None:
        return fallback()
    return handler()


def _assess_requirement(
    text: str,
    *,
    project_info: dict,
    planner: str,
    qa_history: Optional[list[dict]] = None,
    original_title: str = "",
    clarify_answers: Optional[list[dict]] = None,
    last_questions: Optional[list[dict]] = None,
) -> dict:
    actions = _actions()
    return actions.assess_requirement_for_planning(
        text,
        project_info=project_info,
        planner=planner,
        qa_history=qa_history,
        original_title=original_title,
        last_questions=last_questions,
        clarify_answers=clarify_answers,
        clarify_fn=actions.clarify_requirement,
    )


def _raise_clarification_error(outcome: dict | ClarificationTransition) -> None:
    if isinstance(outcome, ClarificationTransition):
        if outcome.status == "interrupt":
            raise RuntimeError("澄清流程已中断，请重新发起需求。")
        raise RuntimeError(outcome.message or "澄清评估失败。")
    if outcome.get("error_kind") == "interrupt":
        raise RuntimeError("澄清流程已中断，请重新发起需求。")
    raise RuntimeError(outcome.get("message") or "澄清评估失败。")


def _assess_or_continue_requirement(
    text: str,
    *,
    project_info: dict,
    planner: str,
    qa_history: Optional[list[dict]] = None,
    original_title: str = "",
    clarify_answers: Optional[list[dict]] = None,
    last_questions: Optional[list[dict]] = None,
    intent: str = "requirement",
) -> dict:
    normalized_original = normalize_requirement_text(original_title)
    if not normalized_original:
        return _assess_requirement(
            text,
            project_info=project_info,
            planner=planner,
            qa_history=qa_history,
            original_title="",
            clarify_answers=clarify_answers,
            last_questions=last_questions,
        )

    actions = _actions()
    pending_state = actions.build_clarification_state(
        original_title=normalized_original,
        qa_history=qa_history,
        last_questions=last_questions,
        intent=intent,
    )
    outcome = actions.continue_pending_clarification(
        pending_state,
        answer=text,
        clarify_answers=clarify_answers,
        project_info=project_info,
        planner=planner,
        intent=intent,
        clarify_fn=actions.clarify_requirement,
    )
    transition = interpret_clarification_outcome(
        outcome,
        pending_state=pending_state,
        fallback_title=normalized_original,
        normalize_text=normalize_requirement_text,
    )
    if transition.status in {"interrupt", "error"}:
        _raise_clarification_error(transition)
    if transition.status == "needs_clarification":
        next_state = transition.pending_state or pending_state
        return {
            "status": "needs_clarification",
            "questions": list(transition.questions) or next_state.get("last_questions") or [],
            "seed_title": next_state.get("original_title") or normalized_original,
            "qa_history": next_state.get("qa_history") or [],
        }

    refined = transition.refined_title or normalized_original
    assessment = outcome.get("assessment") or {}
    return {
        "status": "ready",
        "refined_title": refined,
        "seed_title": assessment.get("seed_title") or normalized_original,
        "qa_history": assessment.get("qa_history") or pending_state.get("qa_history") or [],
    }


def _goal_clarify_payload(*, seed_title: str, questions: list[dict], qa_history: Optional[list[dict]] = None) -> dict:
    return {
        "ok": True,
        "intent": "clarify",
        "questions": questions,
        "original_title": seed_title,
        "qa_history": qa_history or [],
        "message": "为了更好地规划，请先回答几个问题。",
        "workflow_session": build_workflow_session_record(
            phase="clarify", intent="requirement", next_action="clarify",
        ),
    }


def retry_task_action(task_id: int) -> dict:
    """Retry a task and ask the independent daemon to pick it up."""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    if task["status"] == "in_progress":
        raise RuntimeError(f"任务 #{task_id} 正在运行中，请先停止再重试。")
    if task["status"] == "done":
        raise RuntimeError(f"任务 #{task_id} 已完成，不能直接重试。")
    updated = db.reset_task_for_retry(task_id)
    project_name = task["project"]
    service_status, service_error = _request_task_service_start(project_name)
    if service_error:
        _append_event(
            f"任务 #{task_id} 已重试，但任务执行服务启动失败：{service_error}",
            level="error",
            project=project_name,
            task_id=task_id,
        )
        message = f"任务 #{task_id} 已重试，但任务执行服务启动失败：{service_error}"
    else:
        suffix = "已启动" if service_status and service_status.get("started") else "已在运行"
        _append_event(f"任务 #{task_id} 已重试，任务执行服务{suffix}。", project=project_name, task_id=task_id)
        message = f"任务 #{task_id} 已重试，任务执行服务{suffix}。"

    return {
        "ok": True,
        "message": message,
        "task": _task_payload(updated),
        "service": service_status,
        "service_error": service_error,
    }


def promote_task_action(task_id: int) -> dict:
    """Promote a task to P0 and ask the independent daemon to pick it up."""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    if task["status"] == "done":
        raise RuntimeError(f"任务 #{task_id} 已完成，不能插队。")
    if task["status"] == "in_progress":
        raise RuntimeError(f"任务 #{task_id} 正在执行中，无需插队。")
    if task["status"] in {"failed", "cancelled"}:
        task = db.reset_task_for_retry(task_id, reset_retry_count=False)
    updated = db.update_task(task_id, priority="P0", status="backlog")
    project_name = task["project"]
    service_status, service_error = _request_task_service_start(project_name)
    if service_error:
        _append_event(
            f"任务 #{task_id} 已插队到 P0，但任务执行服务启动失败：{service_error}",
            level="error",
            project=project_name,
            task_id=task_id,
        )
        message = f"任务 #{task_id} 已提升到 P0，但任务执行服务启动失败：{service_error}"
    else:
        suffix = "已启动" if service_status and service_status.get("started") else "已在运行"
        _append_event(f"任务 #{task_id} 已插队到 P0，任务执行服务{suffix}。", project=project_name, task_id=task_id)
        message = f"任务 #{task_id} 已提升到 P0，任务执行服务{suffix}。"

    return {
        "ok": True,
        "message": message,
        "task": _task_payload(updated),
        "service": service_status,
        "service_error": service_error,
    }


def split_task_action(task_id: int) -> dict:
    """Re-plan an existing task into smaller subtasks."""
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    project = task["project"]
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    title = task["title"] or ""
    content_hint = (task.get("content") or "").strip()
    if content_hint:
        expanded_title = f"{title}（拆分更细；原任务内容摘要：{content_hint[:400]}）"
    else:
        expanded_title = title

    db.update_task(
        task_id,
        status="cancelled",
        error_message="由用户请求拆分为更小任务，原任务已关闭",
    )
    _append_event(
        f"任务 #{task_id} 已请求拆分，原任务关闭",
        level="warning",
        project=project,
        task_id=task_id,
    )

    result = _actions().submit_requirement_action(
        project,
        expanded_title,
        execute=False,
        max_tasks=5,
        run_async=True,
        clarify=False,
    )
    result["intent"] = "split"
    result["original_task_id"] = task_id
    result["message"] = f"任务 #{task_id} 已关闭，正在规划新的细粒度任务（后台任务 #{result.get('job', {}).get('id')}）。"
    return result


def _job_result_summary(result: dict, execute: bool) -> str:
    task_count = len(result.get("tasks") or [])
    parts = [f"创建 {task_count} 个任务"]
    if result.get("summary"):
        parts.append(result["summary"])
    run_stats = result.get("run") or {}
    if execute and run_stats:
        parts.append(f"执行结果: done={run_stats.get('done', 0)} failed={run_stats.get('failed', 0)} requeued={run_stats.get('requeued', 0)}")
    run_service = result.get("run_service") or {}
    if execute and run_service:
        state = "已启动" if run_service.get("started") else "已在运行"
        parts.append(f"任务执行服务{state}")
    if execute and result.get("run_service_error"):
        parts.append(f"任务执行服务启动失败: {result['run_service_error']}")
    return " | ".join(parts)


def _job_request_value(request: dict, job: dict, key: str, default=None):
    if key in request and request.get(key) not in (None, ""):
        return request.get(key)
    if key in job and job.get(key) not in (None, ""):
        return job.get(key)
    return default


def _load_requirement_job_context(job_id: int) -> _RequirementJobContext:
    job_id = int(job_id)
    db.init_db()
    job = _get_job(job_id)
    if not job:
        raise RuntimeError(f"需求 #{job_id} 不存在。")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    project = str(_job_request_value(request, job, "project", ""))
    title = str(_job_request_value(request, job, "title", "")).strip()
    if not project or not title:
        raise RuntimeError(f"需求 #{job_id} 缺少项目或标题。")
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    return _RequirementJobContext(
        job_id=job_id,
        job=job,
        request=request,
        project=project,
        title=title,
        project_info=project_info,
        execute=bool(_job_request_value(request, {}, "execute", True)),
        planner=str(_job_request_value(request, job, "planner", "codex")),
        agent=request.get("agent") or None,
        priority=str(_job_request_value(request, job, "priority", "P2")),
        max_tasks=int(_job_request_value(request, {}, "max_tasks", 5) or 5),
        executor=str(_job_request_value(request, {}, "executor", "auto")),
        auto_commit=bool(_job_request_value(request, {}, "auto_commit", False)),
        max_retries=int(_job_request_value(request, {}, "max_retries", 3) or 3),
        task_source=str(_job_request_value(request, {}, "task_source", "user")),
    )


def _finalize_cancelled_requirement_job(
    context: _RequirementJobContext,
    append_job_log: Callable[[str], None],
) -> dict | None:
    current = _get_job(context.job_id) or {}
    if str(current.get("status") or "") == "cancelled" and current.get("finished_at"):
        return current
    append_job_log("已按用户请求停止需求规划。")
    updated = _update_job(
        context.job_id,
        status="cancelled",
        phase="cancelled",
        updated_at=_now_iso(),
        finished_at=_now_iso(),
        cancel_requested=True,
        error="用户请求停止需求规划",
    )
    _append_event(f"需求 #{context.job_id} 已停止：{context.title}", level="warning", project=context.project)
    return updated


def _start_requirement_job(context: _RequirementJobContext, append_job_log: Callable[[str], None]) -> None:
    _update_job(context.job_id, status="running", phase="planning", updated_at=_now_iso())
    append_job_log(f"开始规划：{context.title}")
    append_job_log(f"使用规划器：{context.planner}")


def _start_requirement_job_heartbeat(
    context: _RequirementJobContext,
    append_job_log: Callable[[str], None],
):
    import time

    heartbeat_stop = _actions().threading.Event()

    def _job_heartbeat() -> None:
        started = time.monotonic()
        while not heartbeat_stop.wait(15):
            current = _get_job(context.job_id)
            if not current:
                return
            if str(current.get("status") or "") not in {"running", "planning", "cancelling"}:
                return
            elapsed = int(time.monotonic() - started)
            phase = str(current.get("phase") or "planning")
            append_job_log(
                f"规划仍在进行：已等待 {elapsed}s，当前阶段 {phase}；"
                "如果底层 AI 暂时没有输出，页面会继续保持心跳。"
            )

    heartbeat_thread = _actions().threading.Thread(
        target=_job_heartbeat,
        name=f"codepilot-requirement-job-heartbeat-{context.job_id}",
        daemon=True,
    )
    heartbeat_thread.start()
    return heartbeat_stop


def _append_requirement_bus_event(event: dict, append_job_log: Callable[[str], None]) -> None:
    stage = event.get("stage") or "?"
    message = event.get("message") or ""
    extra = event.get("extra") or {}
    if stage in {"ui-state", "job-log"} or extra.get("kind") in {"event", "job", "job_log"}:
        return
    if extra.get("task_log_stream"):
        return
    round_hint = ""
    if "round" in extra and "round_total" in extra:
        round_hint = f" (round {extra['round']}/{extra['round_total']})"
    append_job_log(f"[{stage}{round_hint}] {message}")


def _execute_requirement_planning(context: _RequirementJobContext, shell) -> dict:
    return shell.run_requirement_workflow(
        project_info=db.get_project(context.project) or context.project_info,
        title=context.title,
        planner=context.planner,
        task_agent=context.agent or None,
        priority=context.priority,
        max_tasks=context.max_tasks,
        execute=False,
        executor=context.executor,
        auto_commit=context.auto_commit,
        max_retries=context.max_retries,
        json_mode=False,
        task_source=context.task_source,
    )


def _start_requirement_tasks_if_needed(
    context: _RequirementJobContext,
    result: dict,
    task_ids: list[int],
    append_job_log: Callable[[str], None],
) -> str:
    if not context.execute or not task_ids:
        return ""
    service_status, service_error = _request_task_service_start(context.project)
    if service_error:
        append_job_log(f"任务执行服务启动失败：{service_error}")
        result["run_service_error"] = service_error
        return service_error
    result["run_service"] = service_status or {}
    state = "已启动" if service_status and service_status.get("started") else "已在运行"
    append_job_log(f"任务执行服务{state}，等待 daemon 领取 backlog")
    return ""


def _finalize_requirement_job_success(
    context: _RequirementJobContext,
    result: dict,
    task_ids: list[int],
    service_error: str,
) -> dict:
    status = "attention" if service_error else "succeeded"
    updated = _update_job(
        context.job_id,
        status=status,
        phase="done" if status == "succeeded" else "attention",
        updated_at=_now_iso(),
        finished_at=_now_iso(),
        summary=_job_result_summary(result, context.execute),
        task_ids=task_ids,
        error=service_error,
    )
    _append_event(
        f"需求处理完成：{context.title}",
        level="warning" if status == "attention" else "info",
        project=context.project,
        task_id=task_ids[0] if task_ids else None,
    )
    return updated


def _finalize_requirement_job_failure(
    context: _RequirementJobContext,
    exc: Exception,
    append_job_log: Callable[[str], None],
) -> dict:
    append_job_log(f"失败：{exc}")
    updated = _update_job(
        context.job_id,
        status="failed",
        phase="failed",
        updated_at=_now_iso(),
        finished_at=_now_iso(),
        error=str(exc),
    )
    _append_event(f"需求执行失败：{context.title} | {exc}", level="error", project=context.project)
    return updated


def _run_requirement_job_steps(
    context: _RequirementJobContext,
    shell,
    append_job_log: Callable[[str], None],
) -> dict | None:
    if _is_job_cancel_requested(context.job_id):
        return _finalize_cancelled_requirement_job(context, append_job_log)
    _start_requirement_job(context, append_job_log)
    result = _execute_requirement_planning(context, shell)
    if _is_job_cancel_requested(context.job_id):
        return _finalize_cancelled_requirement_job(context, append_job_log)
    task_ids = [item["id"] for item in (result.get("tasks") or [])]
    append_job_log(f"规划完成，创建 {len(task_ids)} 个任务")
    if context.execute and task_ids and _is_job_cancel_requested(context.job_id):
        return _finalize_cancelled_requirement_job(context, append_job_log)
    service_error = _start_requirement_tasks_if_needed(context, result, task_ids, append_job_log)
    return _finalize_requirement_job_success(context, result, task_ids, service_error)


def run_requirement_job_worker(job_id: int) -> dict | None:
    """Run one persisted requirement-planning job in this process."""
    shell = _shell()
    import codepilot.ai_support.service as _ai_module
    from codepilot.core import progress_bus

    context = _load_requirement_job_context(job_id)

    def _append_job_log(line: str) -> None:
        _append_requirement_job_log(context.job_id, line, project=context.project)

    heartbeat_stop = _start_requirement_job_heartbeat(context, _append_job_log)
    prev_callback = _ai_module._planner_progress_callback
    _ai_module._planner_progress_callback = _append_job_log

    try:
        with progress_bus.subscription(lambda event: _append_requirement_bus_event(event, _append_job_log)):
            try:
                return _run_requirement_job_steps(context, shell, _append_job_log)
            except Exception as exc:
                if _is_job_cancel_requested(context.job_id):
                    return _finalize_cancelled_requirement_job(context, _append_job_log)
                return _finalize_requirement_job_failure(context, exc, _append_job_log)
    finally:
        heartbeat_stop.set()
        _ai_module._planner_progress_callback = prev_callback


def submit_requirement_action(
    project: str,
    title: str,
    *,
    execute: bool = True,
    planner: str | None = None,
    agent: str | None = None,
    priority: str = "P2",
    max_tasks: int = 5,
    executor: str = "auto",
    auto_commit: bool = False,
    max_retries: int = 3,
    run_async: bool = True,
    qa_history: Optional[list[dict]] = None,
    original_title: str = "",
    clarify_answers: Optional[list[dict]] = None,
    clarify_questions: Optional[list[dict]] = None,
    clarify: bool = True,
    task_source: str = "user",
) -> dict:
    shell = _shell()
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    normalized_title = normalize_requirement_text(title)
    normalized_clarify_answers = clarify_answers if isinstance(clarify_answers, list) else []
    normalized_clarify_questions = normalize_clarification_questions(clarify_questions)
    if not normalized_title and normalized_clarify_answers:
        normalized_title = build_clarification_input_summary(
            normalized_clarify_questions,
            raw_answers=normalized_clarify_answers,
        )
    if not normalized_title:
        raise RuntimeError("需求文本不能为空。")
    normalized_priority = (priority or "P2").upper()
    if normalized_priority not in {"P0", "P1", "P2", "P3"}:
        raise RuntimeError("优先级只支持 P0 / P1 / P2 / P3。")
    effective_planner = _effective_planner(project_info, planner)

    if clarify:
        assessment = _assess_or_continue_requirement(
            normalized_title,
            project_info=project_info,
            planner=effective_planner,
            qa_history=normalize_clarification_history(qa_history),
            original_title=original_title,
            clarify_answers=normalized_clarify_answers,
            last_questions=normalized_clarify_questions,
            intent="requirement",
        )
        seed_title = assessment.get("seed_title") or normalized_title
        if assessment.get("status") == "needs_clarification":
            questions = assessment.get("questions") or []
            _append_event(f"需求需要澄清：{seed_title[:60]}", project=project)
            return _goal_clarify_payload(
                seed_title=seed_title,
                questions=questions,
                qa_history=assessment.get("qa_history") or [],
            )
        normalized_title = assessment.get("refined_title") or seed_title

    job_id = _next_job_id()
    with shell._UI_LOCK:
        shell._UI_JOBS[job_id] = {
            "id": job_id,
            "type": "requirement",
            "project": project,
            "title": normalized_title,
            "planner": effective_planner,
            "agent": (agent or "").lower(),
            "priority": normalized_priority,
            "status": "queued",
            "phase": "queued",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "finished_at": "",
            "summary": "",
            "error": "",
            "cancel_requested": False,
            "runner_pid": 0,
            "planner_pids": [],
            "task_ids": [],
            "log": [],
            "request": {
                "project": project,
                "title": normalized_title,
                "execute": bool(execute),
                "planner": effective_planner,
                "agent": agent or None,
                "priority": normalized_priority,
                "max_tasks": int(max_tasks),
                "executor": executor,
                "auto_commit": bool(auto_commit),
                "max_retries": int(max_retries),
                "task_source": task_source or "user",
            },
        }
    _update_job(job_id)
    _append_event(f"收到需求：{normalized_title}", project=project)

    if run_async:
        try:
            process = _start_requirement_job_process(job_id, project_info)
        except Exception as exc:
            _append_requirement_job_log(job_id, f"独立需求规划进程启动失败：{exc}", project=project)
            _update_job(
                job_id,
                status="failed",
                phase="failed",
                updated_at=_now_iso(),
                finished_at=_now_iso(),
                error=f"独立需求规划进程启动失败：{exc}",
            )
        else:
            _update_job(job_id, runner_pid=int(process.pid or 0), phase="queued", updated_at=_now_iso())
            _append_requirement_job_log(job_id, f"独立需求规划工作进程已启动 PID={process.pid}", project=project)
    else:
        run_requirement_job_worker(job_id)

    return {"ok": True, "message": f"需求已提交，后台任务 #{job_id} 已启动。", "job": dict(shell._UI_JOBS[job_id])}


def cancel_job_action(job_id: int) -> dict:
    updated = cancel_ui_job(int(job_id))
    return {
        "ok": True,
        "message": f"需求 #{job_id} 已停止。",
        "job": updated,
    }


def retry_job_action(job_id: int) -> dict:
    job = _get_job(int(job_id))
    if not job:
        raise RuntimeError(f"需求 #{job_id} 不存在。")
    if str(job.get("status") or "") in {"queued", "running", "planning", "cancelling"}:
        raise RuntimeError(f"需求 #{job_id} 仍在处理中，不能重试。")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    if not request:
        raise RuntimeError(f"需求 #{job_id} 缺少可重试的原始参数。")
    _append_event(
        f"需求 #{job_id} 已发起重试：{request.get('title') or job.get('title') or ''}",
        project=str(request.get("project") or job.get("project") or ""),
    )
    return submit_requirement_action(
        str(request.get("project") or job.get("project") or ""),
        str(request.get("title") or job.get("title") or ""),
        execute=bool(request.get("execute", True)),
        planner=str(request.get("planner") or job.get("planner") or "") or None,
        agent=request.get("agent") or None,
        priority=str(request.get("priority") or job.get("priority") or "P2"),
        max_tasks=int(request.get("max_tasks") or 5),
        executor=str(request.get("executor") or "auto"),
        auto_commit=bool(request.get("auto_commit", False)),
        max_retries=int(request.get("max_retries") or 3),
        run_async=True,
        clarify=False,
    )


def _dispatch_goal_command(ctx: _GoalDispatchContext) -> dict:
    _append_event(f"收到命令类输入（已提示用户使用 CLI）：{ctx.text[:60]}", project=ctx.project)
    return {
        "ok": True,
        "intent": "command",
        "message": _actions().command_intent_guidance(),
        "workflow_session": build_workflow_session_record(
            phase="command", intent="command", next_action="guidance",
        ),
    }


def _dispatch_goal_question(ctx: _GoalDispatchContext) -> dict:
    answer = _answer_project_question(
        ctx.project_info,
        ctx.text,
        gateway_options=ctx.gateway_options,
    )
    _append_event(f"回答问题：{ctx.text[:60]}", project=ctx.project)
    return {
        "ok": True,
        "intent": "question",
        "message": answer,
        "workflow_session": build_workflow_session_record(
            phase="question", intent="question", next_action="answer",
        ),
    }


def _dispatch_goal_requirement(ctx: _GoalDispatchContext, *, intent: str) -> dict:
    assessment = _assess_or_continue_requirement(
        ctx.text,
        project_info=ctx.project_info,
        planner=ctx.planner,
        qa_history=ctx.qa_history,
        original_title=ctx.original_title,
        clarify_answers=ctx.clarify_answers,
        last_questions=ctx.clarify_questions,
        intent=intent,
    )
    seed_title = assessment.get("seed_title") or ctx.text
    if assessment.get("status") == "needs_clarification":
        _append_event(f"需求需要澄清：{seed_title[:60]}", project=ctx.project)
        return _goal_clarify_payload(
            seed_title=seed_title,
            questions=assessment.get("questions") or [],
            qa_history=assessment.get("qa_history") or [],
        )

    refined = assessment.get("refined_title") or seed_title
    max_tasks = 1 if intent == "task" else 5
    result, _, _ = _submit_requirement_from_message(
        ctx.project,
        refined,
        planner=ctx.planner,
        max_tasks=max_tasks,
    )
    result["intent"] = intent
    result["refined_title"] = refined
    result["workflow_session"] = build_workflow_session_record(
        phase="plan", intent=intent, next_action="execute",
    )
    return result


def _requirement_confirmation_payload(intent: str) -> dict:
    label = "任务" if intent == "task" else "需求"
    prefix = "任务" if intent == "task" else "需求"
    symbol = "!" if intent == "task" else "#"
    return {
        "ok": True,
        "intent": "confirm",
        "message": (
            f"这条消息更像要创建{label}，但当前不会直接执行。"
            f"如果你确认要创建，请明确发送 `{prefix} <内容>` 或 `{symbol} <内容>`。"
        ),
        "workflow_session": build_workflow_session_record(
            phase="intake", intent=intent, next_action="confirm",
        ),
    }


def _resolve_goal_intent(ctx: _GoalDispatchContext) -> str:
    actions = _actions()
    return resolve_turn_intent(
        ctx.text,
        category=ctx.category,
        forced_intent=ctx.forced_intent or ("requirement" if ctx.original_title else None),
        classify_fn=actions.classify_entry_intent,
        classify_kwargs={
            "project_info": ctx.project_info,
            "category": ctx.category,
            "gateway_options": ctx.gateway_options,
        },
        fallback_intent="question",
    )


def _dispatch_goal_by_intent(ctx: _GoalDispatchContext) -> dict:
    intent = _resolve_goal_intent(ctx)
    if ctx.category == "auto" and not ctx.forced_intent and intent in {"requirement", "task"}:
        return _requirement_confirmation_payload(intent)
    return _dispatch_with_intent_handlers(
        intent,
        handlers={
            "command": lambda: _dispatch_goal_command(ctx),
            "question": lambda: _dispatch_goal_question(ctx),
        },
        fallback=lambda: _dispatch_goal_requirement(ctx, intent=intent),
    )


def artifact_next_actions_for_type(artifact_type: str) -> list[dict]:
    """Return the predefined next actions available for a given artifact type.

    These describe the recommended follow-up steps after a clarify or plan
    artifact has been created.  The actual ``suggested_command`` templates
    should be resolved against the real artifact path before use.
    """
    _defs: dict[str, list[dict]] = {
        "clarify": [
            {
                "id": "plan_from_spec",
                "label": "根据当前 clarify spec 生成执行计划",
                "risk": "low",
                "suggested_command": "codepilot plan -p {project} --from-spec {artifact_path} --json",
            },
            {
                "id": "submit_requirement",
                "label": "提交为需求并创建 backlog 任务",
                "risk": "medium",
                "suggested_command": 'codepilot go "{summary}" -p {project} --json',
            },
            {
                "id": "continue_clarify",
                "label": "继续澄清，补充更多细节",
                "risk": "low",
                "suggested_command": 'codepilot clarify -p {project} "补充：..." --json',
            },
        ],
        "plan": [
            {
                "id": "import_tasks",
                "label": "将候选任务导入 backlog",
                "risk": "medium",
                "suggested_command": "codepilot add -p {project} -f {task_batch_path}",
            },
            {
                "id": "continue_clarify",
                "label": "对计划中不清晰的部分进一步澄清",
                "risk": "low",
                "suggested_command": 'codepilot clarify -p {project} "{summary}" --json',
            },
            {
                "id": "execute_directly",
                "label": "直接执行计划",
                "risk": "high",
                "suggested_command": "codepilot run -p {project} --once --json",
            },
            {
                "id": "abandon_plan",
                "label": "放弃该计划，删除 plan artifact",
                "risk": "low",
                "suggested_command": "rm {plan_path}",
            },
        ],
    }
    return list(_defs.get(artifact_type, []))


def _artifact_type_from_context(context: dict[str, Any]) -> str:
    state = context.get("state") if isinstance(context.get("state"), dict) else {}
    return str(state.get("mode") or context.get("artifact_type") or "").strip().lower()


def _artifact_context_value(context: dict[str, Any], key: str) -> str | None:
    payloads: list[dict[str, Any]] = [context]
    state = context.get("state")
    if isinstance(state, dict):
        payloads.append(state)

    for payload in payloads:
        if key == "spec" and payload.get("artifact_path"):
            return str(payload.get("artifact_path"))
        if key == "task_batch" and payload.get("task_batch_path"):
            return str(payload.get("task_batch_path"))
        artifacts = payload.get("artifact_paths")
        if isinstance(artifacts, dict) and artifacts.get(key):
            return str(artifacts.get(key))
    return None


def _resolve_project_file(project_path: Path, raw_path: str | Path | None, *, label: str) -> Path:
    if raw_path is None or str(raw_path).strip() == "":
        raise RuntimeError(f"artifact context 缺少 {label}。")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = project_path / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(project_path):
        raise RuntimeError(f"{label} 必须指向项目目录内的文件：{resolved}")
    return resolved


def _read_artifact_context(project_info: dict, context_path: str | Path) -> tuple[Path, dict[str, Any]]:
    project_path = Path(project_info["path"]).resolve()
    resolved_context = _resolve_project_file(project_path, context_path, label="artifact context")
    if not resolved_context.is_file():
        raise RuntimeError(f"artifact context 不存在：{resolved_context}")
    try:
        payload = json.loads(resolved_context.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"artifact context 无法读取：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("artifact context 必须是 JSON object。")
    return resolved_context, payload


def _context_next_action(context: dict[str, Any], action_id: str) -> dict[str, Any]:
    wanted = str(action_id or "").strip()
    raw_actions = context.get("next_actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raw_actions = artifact_next_actions_for_type(_artifact_type_from_context(context))
    for item in raw_actions:
        if isinstance(item, dict):
            found = str(item.get("id") or "").strip()
            if found == wanted:
                return dict(item)
        elif isinstance(item, str) and item.strip() == wanted:
            return {"id": wanted, "label": wanted, "risk": "unknown"}
    raise RuntimeError(f"未找到 artifact next_action：{wanted}")


def _execute_artifact_plan_from_spec(project_info: dict, context: dict[str, Any]) -> dict:
    from codepilot.commands.plan import _read_spec, _summary_from_spec, write_plan_artifact

    project_path = Path(project_info["path"]).resolve()
    spec_path = _resolve_project_file(
        project_path,
        _artifact_context_value(context, "spec"),
        label="clarify spec",
    )
    if not spec_path.is_file():
        raise RuntimeError(f"clarify spec 不存在：{spec_path}")
    try:
        resolved_spec, spec_text = _read_spec(project_path, str(spec_path))
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    return write_plan_artifact(
        project_info,
        _summary_from_spec(spec_text),
        source="spec",
        source_path=str(resolved_spec),
        use_wiki=True,
    )


def _execute_artifact_import_tasks(project_info: dict, context: dict[str, Any]) -> dict:
    from codepilot.webapp.action_task_ops import import_tasks_action

    project_path = Path(project_info["path"]).resolve()
    task_batch_path = _resolve_project_file(
        project_path,
        _artifact_context_value(context, "task_batch"),
        label="task batch",
    )
    if not task_batch_path.is_file():
        raise RuntimeError(f"任务批次文件不存在：{task_batch_path}")
    try:
        items = json.loads(task_batch_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"任务批次文件无法读取：{exc}") from exc
    if not isinstance(items, list):
        raise RuntimeError("任务批次文件必须是 JSON array。")
    result = import_tasks_action(str(project_info["name"]), items)
    return {
        **result,
        "task_batch_path": str(task_batch_path),
    }


_ARTIFACT_ACTION_HANDLERS: dict[str, Callable[[dict, dict[str, Any]], dict]] = {
    "plan_from_spec": _execute_artifact_plan_from_spec,
    "import_tasks": _execute_artifact_import_tasks,
}


def execute_artifact_next_action(
    project: str,
    context_path: str,
    action_id: str,
    *,
    allow_high_risk: bool = False,
) -> dict:
    """Execute a Web UI artifact next action through a fixed backend allowlist."""
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    resolved_context, context = _read_artifact_context(project_info, context_path)
    action = _context_next_action(context, action_id)
    normalized_action_id = str(action.get("id") or "").strip()
    risk = str(action.get("risk") or "").strip().lower()
    if risk == "high" and not allow_high_risk:
        raise RuntimeError(f"next_action `{normalized_action_id}` 是高风险动作，默认拒绝执行。")
    handler = _ARTIFACT_ACTION_HANDLERS.get(normalized_action_id)
    if handler is None:
        allowed = ", ".join(sorted(_ARTIFACT_ACTION_HANDLERS))
        raise RuntimeError(f"不支持的 artifact next_action：{normalized_action_id}。当前 allowlist：{allowed}。")

    result = handler(project_info, context)
    payload = {
        "ok": True,
        "project": project_info["name"],
        "artifact_type": _artifact_type_from_context(context),
        "context_path": str(resolved_context),
        "action_id": normalized_action_id,
        "action": action,
        "result": result,
    }
    if result.get("task_batch_path"):
        payload["task_batch_path"] = result["task_batch_path"]
    if result.get("plan_path"):
        payload["plan_path"] = result["plan_path"]
    return payload


def submit_goal_action(
    project: str,
    text: str,
    *,
    category: str = "auto",
    qa_history: Optional[list[dict]] = None,
    original_title: str = "",
    clarify_answers: Optional[list[dict]] = None,
    clarify_questions: Optional[list[dict]] = None,
) -> dict:
    """POST /api/goal — validate payload then delegate to intent handlers."""
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    text = normalize_text(text)
    normalized_clarify_answers = clarify_answers if isinstance(clarify_answers, list) else []
    normalized_clarify_questions = normalize_clarification_questions(clarify_questions)
    if not text and normalized_clarify_answers:
        text = build_clarification_input_summary(
            normalized_clarify_questions,
            raw_answers=normalized_clarify_answers,
        )
    if not text:
        raise RuntimeError("输入不能为空。")
    if len(text.encode("utf-8")) > _GOAL_MAX_BYTES:
        raise RuntimeError(f"输入超过 {_GOAL_MAX_BYTES // 1024}KB 限制。")
    forced_intent, payload_text = parse_intent_prefix(text)
    text = normalize_text(payload_text if forced_intent else text)

    ctx = _GoalDispatchContext(
        project=project,
        project_info=project_info,
        planner=_effective_planner(project_info),
        text=text,
        clarify_answers=normalized_clarify_answers,
        clarify_questions=normalized_clarify_questions,
        category=_normalize_goal_category(category),
        qa_history=normalize_clarification_history(qa_history),
        original_title=original_title,
        gateway_options=_actions().resolve_shared_gateway_options(project_info),
        forced_intent=forced_intent,
    )
    return _actions()._dispatch_goal_by_intent(ctx)
