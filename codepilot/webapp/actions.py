"""State-mutating actions that back the Web UI HTTP endpoints.

This module remains the stable monkeypatch/re-export surface for callers and
tests. The heavy implementations now live in companion modules split by
responsibility:

- :mod:`codepilot.webapp.action_requirements`
- :mod:`codepilot.webapp.action_sessions`
- :mod:`codepilot.webapp.action_task_ops`
"""

from __future__ import annotations

import threading

from codepilot.commands.auto import (  # noqa: F401 - patched in tests
    assess_requirement_for_planning,
    build_clarification_state,
    classify_entry_intent,
    clarify_requirement,
    command_intent_guidance,
    continue_pending_clarification,
    normalize_requirement_text,
    resolve_shared_gateway_options,
    run_requirement_workflow,
)
from codepilot.webapp.action_requirements import (
    _GoalDispatchContext,
    _answer_project_question,
    _assess_or_continue_requirement,
    _assess_requirement,
    _dispatch_goal_by_intent,
    _dispatch_goal_command,
    _dispatch_goal_question,
    _dispatch_goal_requirement,
    _dispatch_with_intent_handlers,
    _format_numbered_questions,
    _goal_clarify_payload,
    _job_result_summary,
    _raise_clarification_error,
    cancel_job_action,
    _resolve_goal_intent,
    retry_job_action,
    _submit_requirement_from_message,
    promote_task_action,
    retry_task_action,
    split_task_action,
    submit_goal_action,
    submit_requirement_action,
)
from codepilot.webapp.action_sessions import (
    _SessionDispatchContext,
    _SessionDispatchDecision,
    _dispatch_session_command,
    _dispatch_session_message,
    _dispatch_session_pending_clarification,
    _dispatch_session_question,
    _dispatch_session_requirement,
    _is_cancel_clarification,
    _legacy_clarification_questions_from_content,
    _message_metadata,
    _message_questions,
    _reconstruct_clarification_state,
    _resolve_session_dispatch_decision,
    _session_payload,
    create_session_action,
    delete_session_action,
    get_session_action,
    list_sessions_action,
    send_session_message_action,
    stop_session_run_action,
)
from codepilot.webapp.action_state import (
    _GOAL_MAX_BYTES,
    _MAX_EVENTS,
    _MAX_JOB_LOG_LINES,
    _append_event,
    _next_job_id,
    _update_job,
    list_ui_events,
    list_ui_jobs,
)
from codepilot.webapp.action_task_ops import (
    archive_task_action,
    batch_task_action,
    cancel_task_action,
    create_project_action,
    create_task_action,
    delete_project_action,
    delete_task_action,
    get_task_template_schema_action,
    import_tasks_action,
    project_service_action,
    stop_task_action,
)


# Fallback in-memory UI state for non-WebUI callers such as Feishu.
# When `codepilot.webapp.server` is loaded, action_state will prefer that shell.
_UI_LOCK = threading.Lock()
_UI_JOB_SEQ = 0
_UI_JOBS: dict[int, dict] = {}
_UI_EVENTS: list[dict] = []
