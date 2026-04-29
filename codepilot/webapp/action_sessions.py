"""Session and clarification actions for the Web UI."""

from __future__ import annotations

import json
import re
import sys
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
from codepilot.webapp.action_state import (
    _append_event,
    _effective_planner,
    _normalize_goal_category,
)
from codepilot.webapp.display_sort import sort_sessions_for_display


_LEGACY_CLARIFY_LINE_RE = re.compile(
    r"^\s*(?:(?:问题\s*)?\d+[\.\)、:：]|[一二三四五六七八九十]+[、.．:：]|[-*])\s*(.+)$"
)


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


def _session_search_terms(query: str) -> list[str]:
    normalized = normalize_text(query).lower()
    return [part for part in re.split(r"\s+", normalized) if part]


def _session_match_snippet(text: str, query: str, *, context: int = 64) -> Optional[str]:
    compact = _compact_session_text(text, limit=600)
    if not compact:
        return None
    lowered = compact.lower()
    terms = _session_search_terms(query)
    if not terms:
        return None

    needle = normalize_text(query).lower()
    index = lowered.find(needle)
    needle_len = len(needle)
    if index < 0:
        for term in terms:
            index = lowered.find(term)
            if index >= 0:
                needle_len = len(term)
                break
    if index < 0:
        return None

    start = max(0, index - context)
    end = min(len(compact), index + needle_len + context)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end].strip()}{suffix}"


def _session_matches_query(session: dict, messages: list[dict], query: str) -> tuple[bool, str, int]:
    terms = _session_search_terms(query)
    if not terms:
        return True, "", 0

    title = str(session.get("title") or "")
    title_haystack = title.lower()
    if all(term in title_haystack for term in terms):
        return True, _session_match_snippet(title, query) or title, 0

    matched_messages = 0
    first_snippet = ""
    for message in messages:
        content = str(message.get("content") or "")
        haystack = " ".join(
            [
                content,
                str(message.get("intent") or ""),
                " ".join(f"#{task_id}" for task_id in _message_task_ids(message)),
            ]
        ).lower()
        if not all(term in haystack for term in terms):
            continue
        matched_messages += 1
        if not first_snippet:
            first_snippet = _session_match_snippet(content, query) or _compact_session_text(content)

    return matched_messages > 0, first_snippet, matched_messages


def _session_list_item(session: dict, messages: list[dict]) -> dict:
    return {
        "id": session["id"],
        "project": session["project"],
        "title": session["title"],
        "status": session["status"],
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "message_count": len(messages),
    }


def list_sessions_action(project: str = "", query: str = "", limit: int = 50) -> dict:
    db.init_db()
    sessions = sort_sessions_for_display(db.list_sessions(project=project or None, status="active"))
    query = normalize_text(query)
    try:
        limit = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        limit = 50

    results = []
    searched = bool(query)
    for session in sessions:
        messages = db.list_session_messages(session["id"])
        item = _session_list_item(session, messages)
        if searched:
            matched, snippet, matched_messages = _session_matches_query(session, messages, query)
            if not matched:
                continue
            item["snippet"] = snippet
            item["matched_message_count"] = matched_messages
        results.append(item)
        if searched and len(results) >= limit:
            break

    return {
        "ok": True,
        "query": query,
        "searched": searched,
        "matched_sessions": len(results) if searched else None,
        "sessions": results,
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
        if parsed.get("metadata"):
            try:
                parsed["metadata"] = json.loads(parsed["metadata"])
            except (json.JSONDecodeError, TypeError):
                parsed["metadata"] = {}
        else:
            parsed["metadata"] = {}
        parsed_messages.append(parsed)
    return {"ok": True, "session": session, "messages": parsed_messages}


def _message_metadata(message: dict) -> dict:
    raw = message.get("metadata")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_task_ids(message: dict) -> list[int]:
    raw = message.get("task_ids")
    if isinstance(raw, list):
        return [int(item) for item in raw if str(item).isdigit()]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(parsed, list):
            ids: list[int] = []
            for item in parsed:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    ids.append(value)
            return ids
    return []


def _compact_session_text(value: str, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _session_history_turns(messages: list[dict], *, limit: int = 8) -> list[dict]:
    turns: list[dict] = []
    pending_user = ""
    for message in messages:
        role = message.get("role")
        content = _compact_session_text(message.get("content") or "", limit=260)
        if not content:
            continue
        if role == "user":
            if pending_user:
                turns.append({"user": pending_user, "assistant": ""})
            pending_user = content
        elif role == "assistant":
            if pending_user:
                turns.append({"user": pending_user, "assistant": content})
                pending_user = ""
            else:
                turns.append({"user": "", "assistant": content})
    if pending_user:
        turns.append({"user": pending_user, "assistant": ""})
    return turns[-limit:]


def _session_context_block(messages: list[dict], *, limit: int = 8, max_chars: int = 1600) -> str:
    relevant = [msg for msg in messages if str(msg.get("content") or "").strip()]
    if not relevant:
        return ""
    lines = []
    for message in relevant[-limit:]:
        role = "用户" if message.get("role") == "user" else "助手"
        intent = message.get("intent") or "-"
        task_ids = _message_task_ids(message)
        task_suffix = f" tasks={task_ids}" if task_ids else ""
        content = _compact_session_text(message.get("content") or "", limit=260)
        lines.append(f"- {role} [{intent}{task_suffix}]: {content}")
    block = "\n".join(lines)
    if len(block) <= max_chars:
        return block
    return block[-max_chars:].lstrip()


def _augment_text_with_session_context(text: str, session_context: str) -> str:
    text = normalize_text(text)
    context = str(session_context or "").strip()
    if not context:
        return text
    if "## 会话上下文" in text:
        return text
    return (
        f"{text}\n\n"
        "## 会话上下文（用于保持连续需求/问题的记忆）\n"
        f"{context}\n\n"
        "请把“当前输入”作为最新指令；如果它引用了上文、上一需求、刚才的计划或已有任务，"
        "必须结合上面的会话上下文理解。"
    )


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
        metadata={"answers": user_answers} if user_answers else None,
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
            },
        )
        return _session_payload("clarify", reply, questions=questions)

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
    return _session_payload("requirement", reply, refined_title=refined, task_ids=task_ids)


def _dispatch_session_command(ctx: _SessionDispatchContext) -> dict:
    reply = _actions().command_intent_guidance()
    db.create_session_message(ctx.session_id, "assistant", reply, intent="command")
    return _session_payload("command", reply)


def _dispatch_session_question(ctx: _SessionDispatchContext) -> dict:
    reply = _answer_project_question(
        ctx.project_info,
        ctx.text,
        gateway_options=ctx.gateway_options,
        history=ctx.session_history,
    )
    db.create_session_message(ctx.session_id, "assistant", reply, intent="question")
    return _session_payload("question", reply)


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
            },
        )
        return _session_payload("clarify", reply, questions=questions)

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
    return _session_payload(intent, reply, refined_title=refined, task_ids=task_ids)


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


def send_session_message_action(
    session_id: int,
    text: str,
    *,
    category: str = "auto",
    clarify_answers: Optional[list[dict]] = None,
) -> dict:
    """Send a message in a session — validate payload then delegate by scenario."""
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    project = session["project"]
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    text = normalize_text(text)
    clarify_answers = clarify_answers if isinstance(clarify_answers, list) else []
    if not text and not clarify_answers:
        raise RuntimeError("输入不能为空。")
    normalized_category = _normalize_goal_category(category)
    forced_intent, payload_text = parse_intent_prefix(text)
    text = normalize_text(payload_text if forced_intent else text)

    existing_messages = db.list_session_messages(session_id)
    pending_state = _reconstruct_clarification_state(existing_messages)
    if not pending_state and _is_cancel_clarification(text):
        db.create_session_message(session_id, "user", "取消本次需求规划")
        reply = "当前没有正在等待澄清的需求规划。"
        db.create_session_message(session_id, "assistant", reply, intent="info")
        return _session_payload("info", reply)
    if not pending_state and clarify_answers:
        reply = "当前没有正在等待回答的澄清问题，请重新提交需求。"
        return _session_payload("info", reply)
    if not existing_messages:
        short_seed = text or build_clarification_input_summary(raw_answers=clarify_answers)
        short_title = short_seed[:40] + ("…" if len(short_seed) > 40 else "")
        db.update_session(session_id, title=short_title)

    ctx = _SessionDispatchContext(
        session_id=session_id,
        project=project,
        project_info=project_info,
        planner=_effective_planner(project_info),
        text=text,
        session_context=_session_context_block(existing_messages),
        session_history=_session_history_turns(existing_messages),
        clarify_answers=clarify_answers,
        category=normalized_category,
        gateway_options=_actions().resolve_shared_gateway_options(project_info),
        forced_intent=forced_intent,
    )
    return _actions()._dispatch_session_message(ctx, existing_messages)


def delete_session_action(session_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    db.delete_session(session_id)
    _append_event(f"删除会话 #{session_id}", project=session["project"])
    return {"ok": True, "message": f"会话 #{session_id} 已删除。"}
