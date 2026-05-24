"""State-mutating actions that back the Web UI HTTP endpoints.

This module remains the stable monkeypatch/re-export surface for callers and
tests. The heavy implementations now live in companion modules split by
responsibility:

- :mod:`codepilot.webapp.action_requirements`
- :mod:`codepilot.webapp.action_session_history`
- :mod:`codepilot.webapp.action_session_records`
- :mod:`codepilot.webapp.action_sessions`
- :mod:`codepilot.webapp.action_task_ops`
"""

from __future__ import annotations

import threading

from codepilot.commands.auto import (  # noqa: F401 - patched in tests
    classify_entry_intent,
    command_intent_guidance,
    normalize_requirement_text,
    resolve_shared_gateway_options,
    run_requirement_workflow,
)


# Fallback in-memory UI state for non-WebUI callers such as Feishu.
# When `codepilot.webapp.server` is loaded, action_state will prefer that shell.
_UI_LOCK = threading.Lock()
_UI_JOB_SEQ = 0
_UI_JOBS: dict[int, dict] = {}
_UI_EVENTS: list[dict] = []
