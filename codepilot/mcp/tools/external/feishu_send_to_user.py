from __future__ import annotations

from typing import Any

from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools.external import (
    PerMinuteRateLimiter,
    ensure_str,
    feishu_rate_limit_error_response,
)


FEISHU_SEND_LIMITER = PerMinuteRateLimiter(limit_per_minute=20)


def _message_card(title: str, message: str) -> dict[str, Any]:
    from codepilot.feishu_cards import _card, _plain_block, _section

    return _card(
        title,
        [_section("消息"), _plain_block(message[:4000])],
        template="blue",
        subtitle="CodePilot 外部集成通知。",
    )


@register_tool(description="Send one direct Feishu card message through the existing bot card API.")
def feishu_send_to_user(
    user_id: str,
    message: str,
    title: str = "CodePilot 通知",
    project: str = "",
) -> dict[str, Any]:
    target = ensure_str(user_id, "user_id", required=True) or ""
    body = ensure_str(message, "message", required=True) or ""
    card_title = ensure_str(title, "title", required=True) or "CodePilot 通知"
    project_name = ensure_str(project, "project", required=False, allow_empty=True) or ""

    decision = FEISHU_SEND_LIMITER.check("feishu_send_to_user")
    if not decision.allowed:
        return feishu_rate_limit_error_response(decision)

    from codepilot import feishu_bot

    sent = feishu_bot._send_bot_card(
        _message_card(card_title, body),
        project_name=project_name,
        chat_ids=[target],
    )
    return {
        "ok": bool(sent),
        "sent": bool(sent),
        "user_id": target,
        "project": project_name,
    }
