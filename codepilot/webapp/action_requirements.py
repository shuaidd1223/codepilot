"""Requirement and task-dispatch actions for the Web UI."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Optional

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
    interpret_clarification_outcome,
    parse_intent_prefix,
    resolve_turn_intent,
)
from codepilot.webapp.action_state import (
    _GOAL_MAX_BYTES,
    _MAX_JOB_LOG_LINES,
    _append_event,
    _effective_planner,
    _extract_job_task_ids,
    _next_job_id,
    _normalize_goal_category,
    _shell,
    _update_job,
)
from codepilot.webapp.payloads import _now_iso, _task_payload


def _actions():
    return sys.modules["codepilot.webapp.actions"]


def _format_numbered_questions(questions: list[dict]) -> str:
    return render_clarification_questions(questions)


def _answer_project_question(project_info: dict, question: str, *, gateway_options=None) -> str:
    from codepilot.ai_support.service import answer_question_via_api

    shared_gateway_options = gateway_options or _actions().resolve_shared_gateway_options(project_info)
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
    }


def retry_task_action(task_id: int) -> dict:
    """Retry a task and immediately kick one backlog pass in background."""
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

            run_backlog(project_name, once=True, limit=1, quiet=True, auto_commit=False)
        except Exception as exc:
            _append_event(
                f"任务 #{task_id} 后台执行失败：{exc}",
                level="error",
                project=project_name,
                task_id=task_id,
            )

    _actions().threading.Thread(
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
    """Promote a task to P0 and kick one backlog pass in background."""
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

            run_backlog(project_name, once=True, limit=1, quiet=True, auto_commit=False)
        except Exception as exc:
            _append_event(
                f"任务 #{task_id} 后台执行失败：{exc}",
                level="error",
                project=project_name,
                task_id=task_id,
            )

    _actions().threading.Thread(
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
        import codepilot.ai_support.service as _ai_module
        from codepilot.core import progress_bus

        _update_job(job_id, status="running", phase="planning", updated_at=_now_iso())
        _append_job_log(f"开始规划：{normalized_title}")
        _append_job_log(f"使用规划器：{effective_planner}")

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
                    task_source=task_source,
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
        _actions().threading.Thread(target=worker, name=f"codepilot-ui-job-{job_id}", daemon=True).start()
    else:
        worker()

    return {"ok": True, "message": f"需求已提交，后台任务 #{job_id} 已启动。", "job": dict(shell._UI_JOBS[job_id])}


def _dispatch_goal_command(ctx: _GoalDispatchContext) -> dict:
    _append_event(f"收到命令类输入（已提示用户使用 CLI）：{ctx.text[:60]}", project=ctx.project)
    return {
        "ok": True,
        "intent": "command",
        "message": _actions().command_intent_guidance(),
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
