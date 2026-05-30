"""Feishu bot constants."""

from __future__ import annotations

_CHAT_CONTEXT_SERVICE = "feishu_chat"
_CHAT_CONTEXT_PREFIX = "chat:"
_NOTIFY_DEDUPE_SERVICE = "feishu_notify"
_INBOUND_DEDUPE_SERVICE = "feishu_inbound_msg"
_PENDING_ACTION_OPTIONS_KEY = "pending_action_options"
_PENDING_GOAL_TEXT_KEY = "pending_goal_text"
_ACTIVE_OPENCODE_SESSION_KEY = "active_opencode_session_id"
_PENDING_CONFIRM_SERVICE = "feishu_confirm"
_PENDING_CONFIRM_DIRECT_SCOPE = "__direct__"
_PENDING_CONFIRM_TTL_SECONDS = 120

_MAX_BATCH_RESULT_LINES = 8
