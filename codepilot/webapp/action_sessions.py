"""Session and clarification actions for the Web UI."""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass
from typing import Optional

from codepilot.storage import database as db
from codepilot.ai_support.clarification_protocol import (
    build_clarification_answer_summary,
    build_clarification_input_summary,
    normalize_clarification_answers,
    normalize_clarification_questions,
    normalize_text,
)
from codepilot.ai_support.interaction_controller import (
    build_workflow_session_record,
    interpret_clarification_outcome,
    parse_intent_prefix,
    resolve_turn_intent,
)
from codepilot.webapp.action_requirements import (
    _answer_project_question,
    _assess_requirement,
    _dispatch_with_intent_handlers,
    _format_numbered_questions,
    _raise_clarification_error,
    _submit_requirement_from_message,
)
from codepilot.webapp.action_session_history import (
    _augment_text_with_session_context,
    _message_metadata,
    _session_message_payload,
)
from codepilot.webapp.action_session_records import (
    create_session_action,
    delete_session_action,
    get_session_action,
    list_sessions_action,
)
from codepilot.webapp.action_state import (
    _effective_planner,
    _normalize_goal_category,
)


_LEGACY_CLARIFY_LINE_RE = re.compile(
    r"^\s*(?:(?:问题\s*)?\d+[\.\)、:：]|[一二三四五六七八九十]+[、.．:：]|[-*])\s*(.+)$"
)
_SESSION_RUNS_LOCK = threading.Lock()
_SESSION_RUNS: dict[tuple[int, int], "_ActiveSessionRun"] = {}


def _actions():
    return sys.modules["codepilot.webapp.actions"]


@dataclass(frozen=True)
class _SessionDispatchContext:
    session_id: int
    project: str
    project_info: dict
    planner: str
    text: str
    session_context: str
    session_history: list[dict]
    clarify_answers: Optional[list[dict]]
    category: str
    gateway_options: object
    forced_intent: Optional[str]


@dataclass(frozen=True)
class _SessionDispatchDecision:
    intent: str
    pending_clarification: Optional[dict] = None


@dataclass
class _ActiveSessionRun:
    session_id: int
    assistant_message_id: int
    project: str
    stop_event: threading.Event
    thread: threading.Thread


def _is_cancel_clarification(text: str) -> bool:
    normalized = normalize_text(text).lower()
    return normalized in {"/clear", "/cancel", "取消", "取消本次规划", "取消当前规划"}


def _session_payload(
    intent: str,
    message: str,
    *,
    task_ids: Optional[list[int]] = None,
    questions: Optional[list[dict]] = None,
    refined_title: str = "",
    workflow_session: Optional[dict] = None,
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
    if workflow_session is not None:
        payload["workflow_session"] = dict(workflow_session)
    return payload


def _legacy_clarification_questions_from_content(content: str) -> list[dict]:
    raw_content = str(content or "")
    questions: list[dict] = []
    for raw_line in raw_content.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.lstrip()
        if len(stripped) != len(raw_line) and re.match(r"^(?:\d+[\.\)、:：]|[-*])\s+", stripped):
            continue
        match = _LEGACY_CLARIFY_LINE_RE.match(raw_line)
        if not match:
            continue
        text = normalize_text(match.group(1))
        if not text:
            continue
        question_index = len(questions) + 1
        questions.append(
            {
                "id": f"legacy_q{question_index}",
                "type": "text",
                "text": text,
                "options": [],
                "allow_free_text": False,
            }
        )
    if questions:
        return questions
    fallback = normalize_text(raw_content)
    if fallback and "\n" not in raw_content:
        return [
            {
                "id": "legacy_q1",
                "type": "text",
                "text": fallback,
                "options": [],
                "allow_free_text": False,
            }
        ]
    return []


def _message_questions(message: dict) -> list[dict]:
    structured = normalize_clarification_questions(_message_metadata(message).get("questions"))
    if structured:
        return structured
    return _legacy_clarification_questions_from_content(message.get("content") or "")


def _reconstruct_clarification_state(messages: list[dict]) -> Optional[dict]:
    if not messages:
        return None
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

    assistant_metadata = _message_metadata(last_assistant)
    original_title = (
        assistant_metadata.get("original_title")
        or (messages[original_user_idx].get("content") or "").strip()
    )
    qa_history: list[dict] = []
    idx = clarify_start
    while idx < last_assistant_idx:
        a_msg = messages[idx]
        u_msg = messages[idx + 1] if idx + 1 < len(messages) else None
        if (
            a_msg.get("role") == "assistant"
            and a_msg.get("intent") == "clarify"
            and u_msg
            and u_msg.get("role") == "user"
        ):
            questions = _message_questions(a_msg)
            answers = normalize_clarification_answers(
                questions,
                raw_answers=_message_metadata(u_msg).get("answers"),
                answer_text=(u_msg.get("content") or "").strip(),
            )
            qa_history.append(
                {
                    "questions": questions,
                    "answers": answers,
                    "answer": build_clarification_answer_summary(answers) or (u_msg.get("content") or "").strip(),
                }
            )
            idx += 2
        else:
            break

    last_questions = _message_questions(last_assistant)
    state = _actions().build_clarification_state(
        original_title=original_title,
        qa_history=qa_history,
        last_questions=last_questions,
        intent="requirement",
    )
    if assistant_metadata.get("session_context"):
        state["session_context"] = assistant_metadata.get("session_context")
    return state


def _dispatch_session_pending_clarification(ctx: _SessionDispatchContext, pending: dict) -> dict:
    if _is_cancel_clarification(ctx.text):
        db.create_session_message(ctx.session_id, "user", "取消本次需求规划")
        reply = "已取消当前这次需求规划，请重新输入新的需求。"
        db.create_session_message(ctx.session_id, "assistant", reply, intent="info")
        return _session_payload("info", reply)

    user_answers = normalize_clarification_answers(
        pending.get("last_questions"),
        raw_answers=ctx.clarify_answers,
        answer_text=ctx.text,
    )
    if not user_answers and not ctx.text:
        return _session_payload("info", "澄清问题已变化或过期，请刷新后重试。")
    user_content = build_clarification_answer_summary(user_answers) or ctx.text
    db.create_session_message(
        ctx.session_id,
        "user",
        user_content,
        metadata={"answers": user_answers, "workflow_phase": "clarify"} if user_answers else {"workflow_phase": "clarify"},
    )
    actions = _actions()
    outcome = actions.continue_pending_clarification(
        pending,
        answer=ctx.text,
        clarify_answers=ctx.clarify_answers,
        project_info=ctx.project_info,
        planner=ctx.planner,
        intent=pending.get("intent") or "requirement",
        clarify_fn=actions.clarify_requirement,
    )
    transition = interpret_clarification_outcome(
        outcome,
        pending_state=pending,
        fallback_title=ctx.text,
        normalize_text=actions.normalize_requirement_text,
    )
    if transition.status in {"interrupt", "error"}:
        _raise_clarification_error(transition)
    if transition.status == "needs_clarification":
        next_state = transition.pending_state or pending
        questions = list(transition.questions) or next_state.get("last_questions") or []
        reply = _format_numbered_questions(questions)
        db.create_session_message(
            ctx.session_id,
            "assistant",
            reply,
            intent="clarify",
            metadata={
                "questions": questions,
                "original_title": next_state.get("original_title") or pending.get("original_title") or ctx.text,
                "session_context": next_state.get("session_context") or pending.get("session_context") or "",
                "workflow_phase": "clarify",
            },
        )
        return _session_payload(
            "clarify", reply, questions=questions,
            workflow_session=build_workflow_session_record(
                phase="clarify", intent="requirement", next_action="clarify",
            ),
        )

    refined = actions.normalize_requirement_text(pending.get("original_title") or "")
    refined = transition.refined_title or refined
    planning_text = _augment_text_with_session_context(
        refined,
        str(pending.get("session_context") or ""),
    )
    _, reply, task_ids = _submit_requirement_from_message(
        ctx.project,
        planning_text,
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
    return _session_payload(
        "requirement", reply, refined_title=refined, task_ids=task_ids,
        workflow_session=build_workflow_session_record(
            phase="plan", intent="requirement", next_action="execute",
        ),
    )


def _dispatch_session_command(ctx: _SessionDispatchContext) -> dict:
    reply = _actions().command_intent_guidance()
    db.create_session_message(ctx.session_id, "assistant", reply, intent="command")
    return _session_payload(
        "command", reply,
        workflow_session=build_workflow_session_record(
            phase="command", intent="command", next_action="guidance",
        ),
    )


def _dispatch_session_question(ctx: _SessionDispatchContext) -> dict:
    reply = _answer_project_question(
        ctx.project_info,
        ctx.text,
        gateway_options=ctx.gateway_options,
        history=ctx.session_history,
    )
    db.create_session_message(ctx.session_id, "assistant", reply, intent="question")
    return _session_payload(
        "question", reply,
        workflow_session=build_workflow_session_record(
            phase="question", intent="question", next_action="answer",
        ),
    )


def _dispatch_session_requirement(ctx: _SessionDispatchContext, *, intent: str) -> dict:
    contextual_text = _augment_text_with_session_context(ctx.text, ctx.session_context)
    assessment = _assess_requirement(
        contextual_text,
        project_info=ctx.project_info,
        planner=ctx.planner,
        clarify_answers=ctx.clarify_answers,
    )
    if assessment.get("status") == "needs_clarification":
        questions = assessment.get("questions") or []
        numbered = _format_numbered_questions(questions)
        reply = (
            "为了更好地规划，请先确认以下几个点：\n" + numbered
            if numbered
            else "为了更好地规划，请先确认以下几个点。"
        )
        db.create_session_message(
            ctx.session_id,
            "assistant",
            reply,
            intent="clarify",
            metadata={
                "questions": questions,
                "original_title": assessment.get("seed_title") or contextual_text,
                "session_context": ctx.session_context,
                "workflow_phase": "clarify",
            },
        )
        return _session_payload(
            "clarify", reply, questions=questions,
            workflow_session=build_workflow_session_record(
                phase="clarify", intent=intent, next_action="clarify",
            ),
        )

    refined = assessment.get("refined_title") or ctx.text
    planning_text = _augment_text_with_session_context(refined, ctx.session_context)
    max_tasks = 1 if intent == "task" else 5
    _, reply, task_ids = _submit_requirement_from_message(
        ctx.project,
        planning_text,
        planner=ctx.planner,
        max_tasks=max_tasks,
    )
    db.create_session_message(ctx.session_id, "assistant", reply, intent=intent, task_ids=task_ids)
    return _session_payload(
        intent, reply, refined_title=refined, task_ids=task_ids,
        workflow_session=build_workflow_session_record(
            phase="plan", intent=intent, next_action="execute",
        ),
    )


def _resolve_session_dispatch_decision(
    ctx: _SessionDispatchContext,
    existing_messages: list[dict],
) -> _SessionDispatchDecision:
    pending = _reconstruct_clarification_state(existing_messages)
    if pending and ctx.category == "auto":
        return _SessionDispatchDecision(
            intent=pending.get("intent") or "requirement",
            pending_clarification=pending,
        )

    actions = _actions()
    intent = resolve_turn_intent(
        ctx.text,
        category=ctx.category,
        forced_intent=ctx.forced_intent,
        classify_fn=actions.classify_entry_intent,
        classify_kwargs={
            "project_info": ctx.project_info,
            "category": ctx.category,
            "gateway_options": ctx.gateway_options,
        },
        fallback_intent="question",
    )
    return _SessionDispatchDecision(intent=intent)


def _session_requirement_confirmation_payload(intent: str) -> dict:
    label = "任务" if intent == "task" else "需求"
    prefix = "任务" if intent == "task" else "需求"
    symbol = "!" if intent == "task" else "#"
    return _session_payload(
        "confirm",
        f"这条消息更像要创建{label}，但当前不会直接执行。请明确发送 `{prefix} <内容>` 或 `{symbol} <内容>` 再继续。",
        workflow_session=build_workflow_session_record(
            phase="intake", intent=intent, next_action="confirm",
        ),
    )


def _dispatch_session_message(ctx: _SessionDispatchContext, existing_messages: list[dict]) -> dict:
    decision = _resolve_session_dispatch_decision(ctx, existing_messages)
    if decision.pending_clarification:
        return _actions()._dispatch_session_pending_clarification(ctx, decision.pending_clarification)
    if ctx.category == "auto" and not ctx.forced_intent and decision.intent in {"requirement", "task"}:
        db.create_session_message(ctx.session_id, "user", ctx.text)
        reply = _session_requirement_confirmation_payload(decision.intent)
        db.create_session_message(ctx.session_id, "assistant", reply["message"], intent="confirm")
        return reply

    db.create_session_message(ctx.session_id, "user", ctx.text)
    return _dispatch_with_intent_handlers(
        decision.intent,
        handlers={
            "command": lambda: _dispatch_session_command(ctx),
            "question": lambda: _dispatch_session_question(ctx),
        },
        fallback=lambda: _dispatch_session_requirement(ctx, intent=decision.intent),
    )


def _emit_session_run_event(
    *,
    project: str,
    session_id: int,
    assistant_message_id: int,
    event_type: str,
    status: str,
    content_delta: str = "",
    content_snapshot: str = "",
    tool_calls: Optional[list[dict]] = None,
    opencode_session_id: str = "",
    error: str = "",
) -> None:
    from codepilot.core import progress_bus

    message = {
        "started": "会话开始处理",
        "delta": "会话输出更新",
        "tool": "会话调用工具",
        "summary": "会话状态更新",
        "done": "会话回复完成",
        "error": "会话回复失败",
        "cancelled": "会话回复已停止",
    }.get(event_type, "会话状态更新")
    extra = {
        "project": project,
        "session_id": session_id,
        "assistant_message_id": assistant_message_id,
        "status": status,
        "content_delta": content_delta,
        "content_snapshot": content_snapshot,
        "tool_calls": tool_calls or [],
        "opencode_session_id": opencode_session_id,
    }
    if error:
        extra["error"] = error
    progress_bus.emit(
        stage="session-run",
        event_type=event_type,
        level="error" if event_type == "error" else "info",
        message=message,
        extra=extra,
    )


def _update_streaming_message(
    assistant_message_id: int,
    *,
    content: str,
    status: str,
    tool_calls: Optional[list[dict]] = None,
    opencode_session_id: str = "",
    intent: str = "streaming",
    error: str = "",
) -> dict | None:
    metadata = {
        "status": status,
        "tool_calls": tool_calls or [],
        "opencode_session_id": opencode_session_id,
    }
    if error:
        metadata["error"] = error
    return db.update_session_message(
        assistant_message_id,
        content=content,
        intent=intent,
        metadata=metadata,
    )


_AGENT_MODE_TO_OPENCODE_AGENT = {
    "codepilot": "codepilot",
    "build": "build",
    "plan": "plan",
    "review": "codepilot",
    "inspect": "codepilot",
    "task": "codepilot",
}

_AGENT_MODE_LABELS = {
    "codepilot": "CodePilot",
    "build": "Build",
    "plan": "Plan",
    "review": "代码审查",
    "inspect": "项目巡检",
    "task": "创建任务",
}

_AGENT_MODE_PROMPT_PREFIX = {
    "review": (
        "工作类型：代码审查。\n"
        "请只读地分析当前仓库的相关改动或被指定的代码片段；"
        "按验收标准、潜在风险、可维护性、测试覆盖给出结构化审查意见，并标明阻塞项。"
        "不要直接修改或写入文件。"
    ),
    "inspect": (
        "工作类型：项目巡检。\n"
        "请走 CodePilot 巡检工作流：使用只读 MCP 工具汇总当前任务/失败/风险/依赖等信号，"
        "输出可执行的下一步建议，不要修改任何代码。"
    ),
    "task": (
        "工作类型：创建任务。\n"
        "请把用户需求拆为结构化 CodePilot 任务：先给出最小可执行的任务清单和验收点，"
        "再通过 CodePilot MCP 把任务落库（包含标题、内容、优先级、agent）。"
    ),
}


def _normalize_session_runtime_config(runtime_config: Optional[dict]) -> dict:
    runtime = runtime_config if isinstance(runtime_config, dict) else {}
    raw = (
        runtime.get("agentMode")
        or runtime.get("agent_mode")
        or runtime.get("taskMode")
        or runtime.get("task_mode")
        or "codepilot"
    )
    agent_mode = str(raw).strip()
    if agent_mode not in _AGENT_MODE_TO_OPENCODE_AGENT:
        agent_mode = "codepilot"
    return {
        "agent_mode": agent_mode,
        "agent": _AGENT_MODE_TO_OPENCODE_AGENT[agent_mode],
    }


def _session_runtime_prompt(text: str, runtime_config: Optional[dict]) -> str:
    runtime = _normalize_session_runtime_config(runtime_config)
    prefix = _AGENT_MODE_PROMPT_PREFIX.get(runtime["agent_mode"])
    if not prefix:
        return text
    return f"{prefix}\n\n用户输入：\n{text}"


def _session_runtime_agent(runtime_config: Optional[dict]) -> str:
    return _normalize_session_runtime_config(runtime_config)["agent"]


def _register_session_run(active: _ActiveSessionRun) -> None:
    with _SESSION_RUNS_LOCK:
        _SESSION_RUNS[(active.session_id, active.assistant_message_id)] = active


def _unregister_session_run(session_id: int, assistant_message_id: int) -> None:
    with _SESSION_RUNS_LOCK:
        _SESSION_RUNS.pop((session_id, assistant_message_id), None)


def _run_session_message_stream(
    *,
    session_id: int,
    project: str,
    text: str,
    assistant_message_id: int,
    stop_event: threading.Event,
    runtime_config: Optional[dict] = None,
) -> None:
    content_snapshot = ""
    tool_calls: list[dict] = []
    opencode_session_id = ""

    def on_stream_event(event: dict) -> None:
        nonlocal content_snapshot, tool_calls, opencode_session_id
        event_type = str(event.get("type") or "summary")
        status = str(event.get("status") or ("done" if event_type == "done" else "running"))
        content_delta = str(event.get("content_delta") or "")
        if event.get("content_snapshot") is not None:
            content_snapshot = str(event.get("content_snapshot") or "")
        elif content_delta:
            content_snapshot += content_delta
        if isinstance(event.get("tool_calls"), list):
            tool_calls = list(event.get("tool_calls") or [])
        opencode_session_id = str(event.get("opencode_session_id") or opencode_session_id)
        if event_type in {"delta", "tool", "summary", "started"}:
            _update_streaming_message(
                assistant_message_id,
                content=content_snapshot,
                status=status,
                tool_calls=tool_calls,
                opencode_session_id=opencode_session_id,
            )
        _emit_session_run_event(
            project=project,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            event_type=event_type,
            status=status,
            content_delta=content_delta,
            content_snapshot=content_snapshot,
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            error=str(event.get("error") or ""),
        )

    try:
        from codepilot.opencode.session import run_opencode_message_stream

        result = run_opencode_message_stream(
            project,
            _session_runtime_prompt(text, runtime_config),
            source="web",
            external_session_id=str(session_id),
            agent=_session_runtime_agent(runtime_config),
            on_event=on_stream_event,
            stop_event=stop_event,
        )
        ok = bool(result.get("ok"))
        opencode_session_id = str(result.get("opencode_session_id") or opencode_session_id)
        tool_calls = list(result.get("tool_calls") or tool_calls)
        final_intent = "opencode" if ok else str(result.get("intent") or "error")
        final_status = "done" if ok else ("cancelled" if final_intent == "cancelled" else "error")
        final_message = str(result.get("message") or content_snapshot or "")
        if final_status == "cancelled" and content_snapshot and final_message not in content_snapshot:
            final_message = content_snapshot.rstrip() + "\n\n（已停止）"
        _update_streaming_message(
            assistant_message_id,
            content=final_message,
            status=final_status,
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            intent=final_intent,
            error="" if ok else final_message,
        )
    except Exception as exc:
        final_message = f"OpenCode 执行失败：{exc}"
        _update_streaming_message(
            assistant_message_id,
            content=final_message,
            status="error",
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            intent="error",
            error=final_message,
        )
        _emit_session_run_event(
            project=project,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            event_type="error",
            status="error",
            content_snapshot=content_snapshot,
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            error=final_message,
        )
    finally:
        _unregister_session_run(session_id, assistant_message_id)


def _start_session_message_stream(
    session_id: int,
    project: str,
    text: str,
    assistant_message_id: int,
    *,
    runtime_config: Optional[dict] = None,
) -> None:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_session_message_stream,
        kwargs={
            "session_id": session_id,
            "project": project,
            "text": text,
            "assistant_message_id": assistant_message_id,
            "stop_event": stop_event,
            "runtime_config": runtime_config,
        },
        daemon=True,
        name=f"codepilot-session-{session_id}-{assistant_message_id}",
    )
    _register_session_run(
        _ActiveSessionRun(
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            project=project,
            stop_event=stop_event,
            thread=thread,
        )
    )
    _emit_session_run_event(
        project=project,
        session_id=session_id,
        assistant_message_id=assistant_message_id,
        event_type="started",
        status="running",
    )
    thread.start()


def send_session_message_action(
    session_id: int,
    text: str,
    *,
    category: str = "auto",
    clarify_answers: Optional[list[dict]] = None,
    run_async: bool = False,
    runtime_config: Optional[dict] = None,
) -> dict:
    """Send a Web UI session message through OpenCode."""
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    project = session["project"]
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    text = normalize_text(text)
    if not text:
        raise RuntimeError("输入不能为空。")
    existing_messages = db.list_session_messages(session_id)
    if not existing_messages:
        short_title = text[:40] + ("…" if len(text) > 40 else "")
        db.update_session(session_id, title=short_title)
    user_message = db.create_session_message(
        session_id, "user", text,
        metadata={"workflow_phase": "intake"},
    )
    if run_async:
        assistant_message = db.create_session_message(
            session_id,
            "assistant",
            "",
            intent="streaming",
            metadata={
                "status": "running",
                "tool_calls": [],
                "opencode_session_id": "",
            },
        )
        assistant_message_id = int(assistant_message["id"])
        normalized_runtime = _normalize_session_runtime_config(runtime_config)
        assistant_message = db.update_session_message(
            assistant_message_id,
            metadata={
                "status": "running",
                "tool_calls": [],
                "opencode_session_id": "",
                "runtime": normalized_runtime,
            },
        ) or assistant_message
        _start_session_message_stream(
            session_id,
            project,
            text,
            assistant_message_id,
            runtime_config=normalized_runtime,
        )
        return {
            "ok": True,
            "intent": "opencode",
            "status": "running",
            "message": "",
            "task_ids": [],
            "user_message_id": int(user_message["id"]),
            "assistant_message_id": assistant_message_id,
            "user_message": _session_message_payload(user_message),
            "assistant_message": _session_message_payload(assistant_message),
        }

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message(
        project,
        _session_runtime_prompt(text, runtime_config),
        source="web",
        external_session_id=str(session_id),
        agent=_session_runtime_agent(runtime_config),
    )
    intent = "opencode" if result.get("ok") else "error"
    reply = str(result.get("message") or "")
    db.create_session_message(
        session_id,
        "assistant",
        reply,
        intent=intent,
        metadata={
            "opencode_session_id": result.get("opencode_session_id") or "",
            "tool_calls": result.get("tool_calls") or [],
        },
    )
    return {
        "ok": bool(result.get("ok")),
        "intent": intent,
        "message": reply,
        "task_ids": [],
        "opencode_session_id": result.get("opencode_session_id") or "",
        "tool_calls": result.get("tool_calls") or [],
    }


def stop_session_run_action(session_id: int, message_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    key = (int(session_id), int(message_id))
    with _SESSION_RUNS_LOCK:
        active = _SESSION_RUNS.get(key)
    if not active:
        raise RuntimeError("当前会话没有可停止的运行。")
    active.stop_event.set()
    _update_streaming_message(
        int(message_id),
        content="正在停止当前回复…",
        status="cancelled",
        intent="cancelled",
    )
    _emit_session_run_event(
        project=active.project,
        session_id=int(session_id),
        assistant_message_id=int(message_id),
        event_type="cancelled",
        status="cancelled",
        content_snapshot="正在停止当前回复…",
    )
    return {"ok": True, "message": "已请求停止当前回复。", "assistant_message_id": int(message_id)}


def update_project_permission_action(project: str, mode: str = "") -> dict:
    """读取或更新项目的 AGENTS.toml [opencode.permission] mode。

    * 若 ``mode`` 为空字符串，只读取当前值并返回。
    * 若 ``mode`` 为 ``ask`` / ``full_access`` / ``custom``，更新配置并写回文件。

    每次读写都会通过 ``_canonical_config`` + ``render_agents_toml`` 标准化整个
    AGENTS.toml，保证格式一致性。
    """
    import tomllib

    from codepilot.commands.config_cmd import _canonical_config, render_agents_toml
    from codepilot.core.config import find_config

    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    project_path = Path(project_info["path"]).resolve()
    config_path = find_config(project_path) or project_path / "AGENTS.toml"
    if not config_path or not config_path.is_file():
        raise RuntimeError(f"项目 '{project}' 没有 AGENTS.toml 配置文件。")

    raw_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        raw_data = {}
    opencode = raw_data.get("opencode") if isinstance(raw_data.get("opencode"), dict) else {}
    permission = opencode.get("permission") if isinstance(opencode.get("permission"), dict) else {}
    current_mode = str(permission.get("mode") or "ask").strip()

    if mode:
        normalized = mode.strip().lower().replace(" ", "_")
        valid_modes = {"ask", "full_access", "custom"}
        if normalized not in valid_modes:
            raise RuntimeError(
                f"无效的权限模式: '{mode}'。仅支持: ask, full_access, custom。"
            )
        if normalized != current_mode:
            if "opencode" not in raw_data or not isinstance(raw_data["opencode"], dict):
                raw_data["opencode"] = {}
            if "permission" not in raw_data["opencode"] or not isinstance(
                raw_data["opencode"]["permission"], dict
            ):
                raw_data["opencode"]["permission"] = {}
            raw_data["opencode"]["permission"]["mode"] = normalized
            canonical = _canonical_config(raw_data, project_name=project_path.name)
            content = render_agents_toml(canonical)
            config_path.write_text(content, encoding="utf-8")
            current_mode = normalized

    return {"ok": True, "mode": current_mode}
