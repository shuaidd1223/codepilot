"""State-mutating actions that back the Web UI HTTP endpoints.

Imported and re-exported by :mod:`codepilot.webui`. The shared UI state
(``_UI_JOBS``, ``_UI_EVENTS``, ``_UI_JOB_SEQ``, ``_UI_LOCK``) lives on the shell
module so tests can reset it directly (e.g. ``webui_mod._UI_JOBS.clear()``,
``webui_mod._UI_JOB_SEQ = 0``); every function in here reaches that state and
the patchable ``run_requirement_workflow`` via ``codepilot.webui``.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from codepilot import db
from codepilot.display_sort import sort_jobs_for_display, sort_sessions_for_display
from codepilot.commands.auto import (  # noqa: F401 — patched in tests
    assess_requirement_for_planning,
    build_clarification_state,
    classify_entry_intent,
    clarify_requirement,
    command_intent_guidance,
    continue_pending_clarification,
    normalize_requirement_text,
    resolve_shared_gateway_options,
)
from codepilot.commands.init import initialize_project
from codepilot.config import load_project_config
from codepilot.interaction_controller import (
    ClarificationTransition,
    interpret_clarification_outcome,
    resolve_turn_intent,
)
from codepilot.webui_payloads import _now_iso, _task_payload


_MAX_EVENTS = 40
_MAX_JOB_LOG_LINES = 50
_GOAL_MAX_BYTES = 4096


def _shell():
    """Return the ``codepilot.webui`` shell module for state/dependency access."""
    return sys.modules["codepilot.webui"]


def _effective_planner(project_info: dict, planner: str | None = None) -> str:
    explicit = (planner or "").strip()
    if explicit:
        return explicit
    cfg = load_project_config(project_info)
    if cfg and getattr(cfg, "automation", None):
        configured = (cfg.automation.planner or "").strip()
        if configured:
            return configured
    return "codex"


def _append_event(message: str, *, level: str = "info", project: str | None = None, task_id: int | None = None) -> None:
    shell = _shell()
    entry = {
        "time": _now_iso(),
        "level": level,
        "project": project or "",
        "task_id": task_id,
        "message": message,
    }
    with shell._UI_LOCK:
        shell._UI_EVENTS.append(entry)
        del shell._UI_EVENTS[:-_MAX_EVENTS]


def _normalize_goal_category(category: str | None) -> str:
    normalized = (category or "auto").lower()
    valid_categories = {"auto", "question", "task", "requirement", "command"}
    if normalized not in valid_categories:
        return "auto"
    return normalized


def _format_numbered_questions(questions: list[str]) -> str:
    return "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))


def _extract_job_task_ids(result: dict) -> list[int]:
    job = result.get("job")
    if not isinstance(job, dict):
        return []
    task_ids = job.get("task_ids") or []
    return task_ids if isinstance(task_ids, list) else []


def _answer_project_question(project_info: dict, question: str, *, gateway_options=None) -> str:
    from codepilot.ai import answer_question_via_api

    shared_gateway_options = gateway_options or resolve_shared_gateway_options(project_info)
    try:
        answer = answer_question_via_api(
            provider_key=shared_gateway_options.classifier_provider,
            question=question,
            gateway_options=shared_gateway_options,
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
    result = submit_requirement_action(
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


@dataclass(frozen=True)
class _GoalDispatchContext:
    project: str
    project_info: dict
    planner: str
    text: str
    category: str
    qa_history: Optional[list[dict]]
    original_title: str
    gateway_options: object


@dataclass(frozen=True)
class _SessionDispatchContext:
    session_id: int
    project: str
    project_info: dict
    planner: str
    text: str
    category: str
    gateway_options: object


@dataclass(frozen=True)
class _SessionDispatchDecision:
    intent: str
    pending_clarification: Optional[dict] = None


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
) -> dict:
    return assess_requirement_for_planning(
        text,
        project_info=project_info,
        planner=planner,
        qa_history=qa_history,
        original_title=original_title,
        clarify_fn=clarify_requirement,
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
        )

    pending_state = build_clarification_state(
        original_title=normalized_original,
        qa_history=qa_history,
        intent=intent,
    )
    outcome = continue_pending_clarification(
        pending_state,
        answer=text,
        project_info=project_info,
        planner=planner,
        intent=intent,
        clarify_fn=clarify_requirement,
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


def _goal_clarify_payload(*, seed_title: str, questions: list[str], qa_history: Optional[list[dict]] = None) -> dict:
    return {
        "ok": True,
        "intent": "clarify",
        "questions": questions,
        "original_title": seed_title,
        "qa_history": qa_history or [],
        "message": "为了更好地规划，请先回答几个问题。",
    }


def _session_payload(
    intent: str,
    message: str,
    *,
    task_ids: Optional[list[int]] = None,
    questions: Optional[list[str]] = None,
    refined_title: str = "",
) -> dict:
    payload: dict = {
        "ok": True,
        "intent": intent,
        "message": message,
        "task_ids": task_ids or [],
    }
    if questions is not None:
        payload["questions"] = questions
    if refined_title:
        payload["refined_title"] = refined_title
    return payload


def _next_job_id() -> int:
    shell = _shell()
    with shell._UI_LOCK:
        shell._UI_JOB_SEQ += 1
        return shell._UI_JOB_SEQ


def _update_job(job_id: int, **fields) -> dict:
    shell = _shell()
    with shell._UI_LOCK:
        job = shell._UI_JOBS[job_id]
        job.update(fields)
        return dict(job)


def list_ui_jobs(project: str | None = None) -> list[dict]:
    shell = _shell()
    with shell._UI_LOCK:
        items = [dict(job) for job in shell._UI_JOBS.values()]
    if project:
        items = [job for job in items if job.get("project") == project]
    return sort_jobs_for_display(items)[:12]


def list_ui_events(project: str | None = None) -> list[dict]:
    shell = _shell()
    with shell._UI_LOCK:
        items = list(shell._UI_EVENTS)
    if project:
        items = [event for event in items if not event.get("project") or event.get("project") == project]
    return list(reversed(items[-12:]))


def create_project_action(path: str, *, name: str = "", no_config: bool = False) -> dict:
    db.init_db()
    raw_path = (path or "").strip().strip('"')
    if not raw_path:
        raise RuntimeError("工作目录不能为空。")
    result = initialize_project(Path(raw_path).expanduser(), name.strip() or None, no_config=no_config)
    project = result["project"]
    action = "注册" if result["created"] else "更新"
    _append_event(f"{action}项目：{project['name']}", project=project["name"])
    return {
        "ok": True,
        "created": bool(result["created"]),
        "message": f"项目 '{project['name']}' 已{action}。",
        "project": project,
        "config_file": result.get("config_file") or "",
    }


def delete_project_action(name: str) -> dict:
    db.init_db()
    project_name = (name or "").strip()
    if not project_name:
        raise RuntimeError("项目名称不能为空。")
    project = db.get_project(project_name)
    if not project:
        raise RuntimeError(f"项目 '{project_name}' 不存在。")
    stats = db.get_task_stats(project_name)
    if not db.delete_project(project_name):
        raise RuntimeError(f"项目 '{project_name}' 删除失败。")
    _append_event(f"删除项目：{project_name}", level="warning", project=project_name)
    return {
        "ok": True,
        "message": f"项目 '{project_name}' 已删除，工作目录保留。",
        "project": project_name,
        "path": project["path"],
        "deleted_tasks": stats["total"],
    }


def project_service_action(project: str, service: str, action: str) -> dict:
    db.init_db()
    project_name = (project or "").strip()
    if not project_name:
        raise RuntimeError("项目名称不能为空。")
    if not db.get_project(project_name):
        raise RuntimeError(f"项目 '{project_name}' 不存在。")
    service = (service or "").strip().lower()
    action = (action or "").strip().lower()
    if service not in {"tasks", "inspect"}:
        raise RuntimeError("服务只支持 tasks / inspect。")
    if action not in {"start", "stop", "status"}:
        raise RuntimeError("操作只支持 start / stop / status。")

    if service == "tasks":
        from codepilot.commands.daemon import daemon_service_status, start_daemon_service, stop_daemon_service
        if action == "start":
            result = start_daemon_service(project=project_name)
            msg = "任务执行服务已启动" if result.get("started") else "任务执行服务已在运行"
        elif action == "stop":
            result = stop_daemon_service(project_name)
            if result.get("stop_requested"):
                msg = "任务轮询已请求停止，当前任务完成后不会继续领取下一个任务"
            else:
                msg = "任务执行服务已停止" if result.get("stopped") else "任务执行服务未运行"
        else:
            result = daemon_service_status(project_name)
            msg = "任务执行服务状态已刷新"
    else:
        from codepilot.commands.inspect import inspect_service_status, start_inspect_service, stop_inspect_service
        if action == "start":
            result = start_inspect_service(project_name)
            msg = "巡检服务已启动" if result.get("started") else "巡检服务已在运行"
        elif action == "stop":
            result = stop_inspect_service(project_name)
            msg = "巡检服务已停止" if result.get("stopped") else "巡检服务未运行"
        else:
            result = inspect_service_status(project_name)
            msg = "巡检服务状态已刷新"

    level = "warning" if action == "stop" else "info"
    _append_event(f"{project_name}: {msg}", level=level, project=project_name)
    return {"ok": True, "message": msg, "project": project_name, "service": service, "action": action, "status": result}


def retry_task_action(task_id: int) -> dict:
    """Retry a task: reset it to backlog AND kick off one execution pass in
    a background thread so the UI feels like "click retry → task starts".

    Before, retry only reset the DB row and waited for an external daemon
    (or a manual ``codepilot run``) to pick it up. Without a running
    daemon, nothing happened — the user saw a green toast but the task
    sat idle. The background :func:`run_backlog` call below is a single-
    pass run (``once=True``, ``limit=1``) so it processes at most one
    runnable task and exits; its own live-task guard ensures it no-ops
    cleanly if a daemon is already running something.
    """
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
    _append_event(f"任务 #{task_id} 已重试，后台开始执行…", project=project_name, task_id=task_id)

    def _run_worker() -> None:
        try:
            from codepilot.commands.run import run_backlog
            run_backlog(project_name, once=True, limit=1, quiet=True)
        except Exception as exc:
            _append_event(
                f"任务 #{task_id} 后台执行失败：{exc}",
                level="error",
                project=project_name,
                task_id=task_id,
            )

    threading.Thread(
        target=_run_worker,
        name=f"codepilot-ui-retry-{task_id}",
        daemon=True,
    ).start()

    return {
        "ok": True,
        "message": f"任务 #{task_id} 已重试，后台开始执行。",
        "task": _task_payload(updated),
    }


def promote_task_action(task_id: int) -> dict:
    """Promote a task to P0 and kick off a run, mirroring retry semantics.

    Without the background kicker, a promote click also just sits in
    backlog until something drains the queue. Since the user's intent is
    clearly "run this next", start one run-pass right after bumping
    priority.
    """
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
    _append_event(f"任务 #{task_id} 已插队到 P0，后台开始执行…", project=project_name, task_id=task_id)

    def _run_worker() -> None:
        try:
            from codepilot.commands.run import run_backlog
            run_backlog(project_name, once=True, limit=1, quiet=True)
        except Exception as exc:
            _append_event(
                f"任务 #{task_id} 后台执行失败：{exc}",
                level="error",
                project=project_name,
                task_id=task_id,
            )

    threading.Thread(
        target=_run_worker,
        name=f"codepilot-ui-promote-{task_id}",
        daemon=True,
    ).start()

    return {
        "ok": True,
        "message": f"任务 #{task_id} 已提升到 P0 并开始执行。",
        "task": _task_payload(updated),
    }


def split_task_action(task_id: int) -> dict:
    """Re-plan an existing task into smaller subtasks.

    The existing task becomes ``cancelled`` (so history is kept) and the
    planner is asked to break its original title / content into concrete
    subtasks. Useful when a task turned out to be too big to tackle in one
    go — the user clicks "拆分" and gets a fresh, finer-grained backlog.
    """
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    project = task["project"]
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    title = task["title"] or ""
    # Inline part of the content as additional context for the planner so
    # the resulting subtasks preserve the original intent (goal / acceptance
    # criteria). We intentionally append rather than replace the title.
    content_hint = (task.get("content") or "").strip()
    if content_hint:
        expanded_title = f"{title}（拆分更细；原任务内容摘要：{content_hint[:400]}）"
    else:
        expanded_title = title

    # Cancel the original so the dashboard reflects the reshape.
    db.update_task(
        task_id,
        status="cancelled",
        error_message="由用户请求拆分为更小任务，原任务已关闭",
    )
    _append_event(
        f"任务 #{task_id} 已请求拆分，原任务关闭",
        level="warning", project=project, task_id=task_id,
    )

    result = submit_requirement_action(
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


def stop_task_action(task_id: int) -> dict:
    from click.testing import CliRunner
    from codepilot.commands.tasks import stop as stop_cmd

    result = CliRunner().invoke(stop_cmd, [str(task_id)])
    if result.exit_code != 0:
        raise RuntimeError(result.output.strip() or f"停止任务 #{task_id} 失败。")
    task = db.get_task(task_id)
    if task:
        _append_event(f"任务 #{task_id} 已收到停止请求。", level="warning", project=task["project"], task_id=task_id)
    return {"ok": True, "message": result.output.strip(), "task": _task_payload(task) if task else None}


def create_task_action(
    project: str,
    title: str,
    *,
    content: str = "",
    priority: str = "P2",
    agent: str | None = None,
    max_retries: int = 3,
) -> dict:
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    normalized_title = " ".join((title or "").split())
    if not normalized_title:
        raise RuntimeError("任务标题不能为空。")
    normalized_priority = (priority or "P2").upper()
    if normalized_priority not in {"P0", "P1", "P2", "P3"}:
        raise RuntimeError("优先级只支持 P0 / P1 / P2 / P3。")
    resolved_agent = (agent or project_info.get("default_mode") or "dual").lower()
    if resolved_agent == "auto":
        resolved_agent = project_info.get("default_mode") or "dual"

    from codepilot.ai import resolve_agent_with_fallback
    default_mode = project_info.get("default_mode") or "dual"
    resolved_agent, fallback_reason = resolve_agent_with_fallback(
        resolved_agent,
        project_path=project_info["path"],
        default_mode=default_mode,
    )

    task = db.create_task(
        project=project,
        title=normalized_title,
        content=content,
        agent=resolved_agent,
        priority=normalized_priority,
        project_path=project_info["path"],
        max_retries=max(0, int(max_retries or 0)),
        fallback_reason=fallback_reason,
    )
    msg = f"任务 #{task['id']} 已创建。"
    if fallback_reason:
        msg += f" （{fallback_reason}）"
    _append_event(f"已新建任务 #{task['id']}：{normalized_title}", project=project, task_id=task["id"])
    return {"ok": True, "message": msg, "task": _task_payload(task)}


def _job_result_summary(result: dict, execute: bool) -> str:
    task_count = len(result.get("tasks") or [])
    parts = [f"创建 {task_count} 个任务"]
    if result.get("summary"):
        parts.append(result["summary"])
    run_stats = result.get("run") or {}
    if execute and run_stats:
        parts.append(f"执行结果: done={run_stats.get('done', 0)} failed={run_stats.get('failed', 0)} requeued={run_stats.get('requeued', 0)}")
    return " | ".join(parts)


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
    clarify: bool = True,
) -> dict:
    shell = _shell()
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    normalized_title = normalize_requirement_text(title)
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
            qa_history=qa_history,
            original_title=original_title,
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
            "task_ids": [],
            "log": [],
        }
    _append_event(f"收到需求：{normalized_title}", project=project)

    def _append_job_log(line: str) -> None:
        with shell._UI_LOCK:
            job = shell._UI_JOBS.get(job_id)
            if job:
                job["log"].append(line)
                if len(job["log"]) > _MAX_JOB_LOG_LINES:
                    del job["log"][:-_MAX_JOB_LOG_LINES]
                job["updated_at"] = _now_iso()

    def worker() -> None:
        import codepilot.ai as _ai_module
        from codepilot import progress_bus

        _update_job(job_id, status="running", phase="planning", updated_at=_now_iso())
        _append_job_log(f"开始规划：{normalized_title}")
        _append_job_log(f"使用规划器：{effective_planner}")

        # Subscribe this job to the progress bus. Every event emitted during
        # this worker's run — regardless of whether it comes from the planner,
        # recon stage, or the builder/reviewer loop — lands in the job log and
        # flows out to any SSE listeners attached to the bus as well.
        def _bus_listener(event: dict) -> None:
            stage = event.get("stage") or "?"
            message = event.get("message") or ""
            extra = event.get("extra") or {}
            if extra.get("task_log_stream"):
                return
            round_hint = ""
            if "round" in extra and "round_total" in extra:
                round_hint = f" (round {extra['round']}/{extra['round_total']})"
            _append_job_log(f"[{stage}{round_hint}] {message}")

        # Keep the old planner callback mirror for now so any code still
        # reading ai_mod._planner_progress_callback (e.g. very old subscribers)
        # doesn't regress.
        prev_callback = _ai_module._planner_progress_callback
        _ai_module._planner_progress_callback = _append_job_log

        with progress_bus.subscription(_bus_listener):
            try:
                result = shell.run_requirement_workflow(
                    project_info=db.get_project(project) or project_info,
                    title=normalized_title,
                    planner=effective_planner,
                    task_agent=agent or None,
                    priority=normalized_priority,
                    max_tasks=max_tasks,
                    execute=execute,
                    executor=executor,
                    auto_commit=auto_commit,
                    max_retries=max_retries,
                    json_mode=False,
                )
                task_ids = [item["id"] for item in (result.get("tasks") or [])]
                run_stats = result.get("run") or {}
                status = "attention" if execute and run_stats.get("failed") else "succeeded"
                _append_job_log(f"规划完成，创建 {len(task_ids)} 个任务")
                if execute and run_stats:
                    _append_job_log(f"执行结果: done={run_stats.get('done', 0)} failed={run_stats.get('failed', 0)}")
                _update_job(
                    job_id,
                    status=status,
                    phase="done" if status == "succeeded" else "attention",
                    updated_at=_now_iso(),
                    finished_at=_now_iso(),
                    summary=_job_result_summary(result, execute),
                    task_ids=task_ids,
                    error="",
                )
                _append_event(
                    f"需求处理完成：{normalized_title}",
                    level="warning" if status == "attention" else "info",
                    project=project,
                    task_id=task_ids[0] if task_ids else None,
                )
            except Exception as exc:
                _append_job_log(f"失败：{exc}")
                _update_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    updated_at=_now_iso(),
                    finished_at=_now_iso(),
                    error=str(exc),
                )
                _append_event(f"需求执行失败：{normalized_title} | {exc}", level="error", project=project)
            finally:
                _ai_module._planner_progress_callback = prev_callback

    if run_async:
        threading.Thread(target=worker, name=f"codepilot-ui-job-{job_id}", daemon=True).start()
    else:
        worker()

    return {"ok": True, "message": f"需求已提交，后台任务 #{job_id} 已启动。", "job": dict(shell._UI_JOBS[job_id])}


def _dispatch_goal_command(ctx: _GoalDispatchContext) -> dict:
    _append_event(f"收到命令类输入（已提示用户使用 CLI）：{ctx.text[:60]}", project=ctx.project)
    return {
        "ok": True,
        "intent": "command",
        "message": command_intent_guidance(),
    }


def _dispatch_goal_question(ctx: _GoalDispatchContext) -> dict:
    answer = _answer_project_question(
        ctx.project_info,
        ctx.text,
        gateway_options=ctx.gateway_options,
    )
    _append_event(f"回答问题：{ctx.text[:60]}", project=ctx.project)
    return {"ok": True, "intent": "question", "message": answer}


def _dispatch_goal_requirement(ctx: _GoalDispatchContext, *, intent: str) -> dict:
    assessment = _assess_or_continue_requirement(
        ctx.text,
        project_info=ctx.project_info,
        planner=ctx.planner,
        qa_history=ctx.qa_history,
        original_title=ctx.original_title,
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
    return result


def _resolve_goal_intent(ctx: _GoalDispatchContext) -> str:
    # Mid-clarification: treat the new text as the user's answer to the prior
    # round and skip re-classification.
    return resolve_turn_intent(
        ctx.text,
        category=ctx.category,
        forced_intent="requirement" if ctx.original_title else None,
        classify_fn=classify_entry_intent,
        classify_kwargs={
            "project_info": ctx.project_info,
            "category": ctx.category,
            "gateway_options": ctx.gateway_options,
        },
        fallback_intent="requirement",
    )


def _dispatch_goal_by_intent(ctx: _GoalDispatchContext) -> dict:
    intent = _resolve_goal_intent(ctx)
    return _dispatch_with_intent_handlers(
        intent,
        handlers={
            "command": lambda: _dispatch_goal_command(ctx),
            "question": lambda: _dispatch_goal_question(ctx),
        },
        fallback=lambda: _dispatch_goal_requirement(ctx, intent=intent),
    )


def submit_goal_action(
    project: str,
    text: str,
    *,
    category: str = "auto",
    qa_history: Optional[list[dict]] = None,
    original_title: str = "",
) -> dict:
    """POST /api/goal — validate payload then delegate to intent handlers."""
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("输入不能为空。")
    if len(text.encode("utf-8")) > _GOAL_MAX_BYTES:
        raise RuntimeError(f"输入超过 {_GOAL_MAX_BYTES // 1024}KB 限制。")

    ctx = _GoalDispatchContext(
        project=project,
        project_info=project_info,
        planner=_effective_planner(project_info),
        text=text,
        category=_normalize_goal_category(category),
        qa_history=qa_history,
        original_title=original_title,
        gateway_options=resolve_shared_gateway_options(project_info),
    )
    return _dispatch_goal_by_intent(ctx)


# ── Session actions ────────────────────────────────────────────────────────


def list_sessions_action(project: str = "") -> dict:
    db.init_db()
    sessions = sort_sessions_for_display(db.list_sessions(project=project or None, status="active"))
    return {
        "ok": True,
        "sessions": [
            {
                "id": s["id"],
                "project": s["project"],
                "title": s["title"],
                "status": s["status"],
                "created_at": s["created_at"],
                "updated_at": s["updated_at"],
                "message_count": len(db.list_session_messages(s["id"])),
            }
            for s in sessions
        ],
    }


def create_session_action(project: str, title: str = "") -> dict:
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    session = db.create_session(project, title=title or "新会话")
    _append_event(f"新建会话 #{session['id']}：{session['title']}", project=project)
    return {"ok": True, "session": session}


def get_session_action(session_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    messages = db.list_session_messages(session_id)
    parsed_messages = []
    for msg in messages:
        parsed = dict(msg)
        if parsed.get("task_ids"):
            try:
                parsed["task_ids"] = json.loads(parsed["task_ids"])
            except (json.JSONDecodeError, TypeError):
                parsed["task_ids"] = []
        else:
            parsed["task_ids"] = []
        parsed_messages.append(parsed)
    return {"ok": True, "session": session, "messages": parsed_messages}


def _reconstruct_clarification_state(messages: list[dict]) -> Optional[dict]:
    """Walk session messages and, if the last assistant turn is a clarify
    request, rebuild normalized clarification state
    ``{original_title, qa_history, last_questions, intent}``.

    Returns ``None`` when the session is not mid-clarification.
    """
    if not messages:
        return None
    # The last *assistant* message determines whether we're waiting on answers.
    last_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break
    if last_assistant_idx is None:
        return None
    last_assistant = messages[last_assistant_idx]
    if last_assistant.get("intent") != "clarify":
        return None

    # Walk back to the first user message of this clarification burst.
    clarify_start = last_assistant_idx
    while clarify_start - 2 >= 0:
        prev_assistant = messages[clarify_start - 2]
        if prev_assistant.get("role") == "assistant" and prev_assistant.get("intent") == "clarify":
            clarify_start -= 2
            continue
        break

    original_user_idx = clarify_start - 1
    if original_user_idx < 0 or messages[original_user_idx].get("role") != "user":
        return None

    original_title = (messages[original_user_idx].get("content") or "").strip()
    # Collect Q/A pairs between the original user message and *last_assistant*
    # (exclusive on the assistant at last_assistant_idx because that one is the
    # *outstanding* question the current user input answers).
    qa_history: list[dict] = []
    # Iterate paired (assistant clarify, user answer) through prior rounds.
    idx = clarify_start
    while idx < last_assistant_idx:
        a_msg = messages[idx]
        u_msg = messages[idx + 1] if idx + 1 < len(messages) else None
        if (
            a_msg.get("role") == "assistant" and a_msg.get("intent") == "clarify"
            and u_msg and u_msg.get("role") == "user"
        ):
            qa_history.append({
                "question": (a_msg.get("content") or "").strip(),
                "answer": (u_msg.get("content") or "").strip(),
            })
            idx += 2
        else:
            break

    last_questions_raw = (last_assistant.get("content") or "").splitlines()
    last_questions = [line.lstrip("0123456789.、 -") for line in last_questions_raw if line.strip()]

    return build_clarification_state(
        original_title=original_title,
        qa_history=qa_history,
        last_questions=last_questions,
        intent="requirement",
    )


def _dispatch_session_pending_clarification(ctx: _SessionDispatchContext, pending: dict) -> dict:
    db.create_session_message(ctx.session_id, "user", ctx.text)
    outcome = continue_pending_clarification(
        pending,
        answer=ctx.text,
        project_info=ctx.project_info,
        planner=ctx.planner,
        intent=pending.get("intent") or "requirement",
        clarify_fn=clarify_requirement,
    )
    transition = interpret_clarification_outcome(
        outcome,
        pending_state=pending,
        fallback_title=ctx.text,
        normalize_text=normalize_requirement_text,
    )
    if transition.status in {"interrupt", "error"}:
        _raise_clarification_error(transition)
    if transition.status == "needs_clarification":
        next_state = transition.pending_state or pending
        questions = list(transition.questions) or next_state.get("last_questions") or []
        reply = _format_numbered_questions(questions)
        db.create_session_message(ctx.session_id, "assistant", reply, intent="clarify")
        return _session_payload("clarify", reply, questions=questions)

    refined = transition.refined_title or normalize_requirement_text(pending.get("original_title") or "")
    _, reply, task_ids = _submit_requirement_from_message(
        ctx.project,
        refined,
        planner=ctx.planner,
        max_tasks=5,
    )
    db.create_session_message(
        ctx.session_id,
        "assistant",
        reply,
        intent="requirement",
        task_ids=task_ids,
    )
    return _session_payload("requirement", reply, refined_title=refined, task_ids=task_ids)


def _dispatch_session_command(ctx: _SessionDispatchContext) -> dict:
    reply = command_intent_guidance()
    db.create_session_message(ctx.session_id, "assistant", reply, intent="command")
    return _session_payload("command", reply)


def _dispatch_session_question(ctx: _SessionDispatchContext) -> dict:
    reply = _answer_project_question(
        ctx.project_info,
        ctx.text,
        gateway_options=ctx.gateway_options,
    )
    db.create_session_message(ctx.session_id, "assistant", reply, intent="question")
    return _session_payload("question", reply)


def _dispatch_session_requirement(ctx: _SessionDispatchContext, *, intent: str) -> dict:
    assessment = _assess_requirement(
        ctx.text,
        project_info=ctx.project_info,
        planner=ctx.planner,
    )
    if assessment.get("status") == "needs_clarification":
        questions = assessment.get("questions") or []
        numbered = _format_numbered_questions(questions)
        reply = (
            "为了更好地规划，请先确认以下几个点：\n" + numbered
            if numbered
            else "为了更好地规划，请先确认以下几个点。"
        )
        db.create_session_message(ctx.session_id, "assistant", reply, intent="clarify")
        return _session_payload("clarify", reply, questions=questions)

    refined = assessment.get("refined_title") or ctx.text
    max_tasks = 1 if intent == "task" else 5
    _, reply, task_ids = _submit_requirement_from_message(
        ctx.project,
        refined,
        planner=ctx.planner,
        max_tasks=max_tasks,
    )
    db.create_session_message(ctx.session_id, "assistant", reply, intent=intent, task_ids=task_ids)
    return _session_payload(intent, reply, refined_title=refined, task_ids=task_ids)


def _resolve_session_dispatch_decision(
    ctx: _SessionDispatchContext,
    existing_messages: list[dict],
) -> _SessionDispatchDecision:
    # ── Multi-turn clarification: continue only for default auto routing. ──
    pending = _reconstruct_clarification_state(existing_messages)
    if pending and ctx.category == "auto":
        return _SessionDispatchDecision(
            intent=pending.get("intent") or "requirement",
            pending_clarification=pending,
        )

    intent = resolve_turn_intent(
        ctx.text,
        category=ctx.category,
        classify_fn=classify_entry_intent,
        classify_kwargs={
            "project_info": ctx.project_info,
            "category": ctx.category,
            "gateway_options": ctx.gateway_options,
        },
        fallback_intent="requirement",
    )
    return _SessionDispatchDecision(intent=intent)


def _dispatch_session_message(ctx: _SessionDispatchContext, existing_messages: list[dict]) -> dict:
    decision = _resolve_session_dispatch_decision(ctx, existing_messages)
    if decision.pending_clarification:
        return _dispatch_session_pending_clarification(ctx, decision.pending_clarification)

    db.create_session_message(ctx.session_id, "user", ctx.text)
    return _dispatch_with_intent_handlers(
        decision.intent,
        handlers={
            "command": lambda: _dispatch_session_command(ctx),
            "question": lambda: _dispatch_session_question(ctx),
        },
        fallback=lambda: _dispatch_session_requirement(ctx, intent=decision.intent),
    )


def send_session_message_action(session_id: int, text: str, *, category: str = "auto") -> dict:
    """Send a message in a session — validate payload then delegate by scenario."""
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    project = session["project"]
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("输入不能为空。")
    normalized_category = _normalize_goal_category(category)

    existing_messages = db.list_session_messages(session_id)
    if not existing_messages:
        short_title = text[:40] + ("…" if len(text) > 40 else "")
        db.update_session(session_id, title=short_title)

    ctx = _SessionDispatchContext(
        session_id=session_id,
        project=project,
        project_info=project_info,
        planner=_effective_planner(project_info),
        text=text,
        category=normalized_category,
        gateway_options=resolve_shared_gateway_options(project_info),
    )
    return _dispatch_session_message(ctx, existing_messages)


def delete_session_action(session_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    db.delete_session(session_id)
    _append_event(f"删除会话 #{session_id}", project=session["project"])
    return {"ok": True, "message": f"会话 #{session_id} 已删除。"}
