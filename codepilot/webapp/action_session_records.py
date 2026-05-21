"""Session record CRUD and listing actions for the Web UI."""

from __future__ import annotations

import json
import re
from typing import Optional

from codepilot.ai_support.clarification_protocol import normalize_text
from codepilot.storage import database as db
from codepilot.webapp.action_session_history import (
    _compact_session_text,
    _message_task_ids,
)
from codepilot.webapp.action_state import _append_event
from codepilot.webapp.display_sort import sort_sessions_for_display


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


def delete_session_action(session_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    db.delete_session(session_id)
    _append_event(f"删除会话 #{session_id}", project=session["project"])
    return {"ok": True, "message": f"会话 #{session_id} 已删除。"}
