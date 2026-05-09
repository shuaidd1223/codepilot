from __future__ import annotations


def test_feishu_rate_limiter_allows_configured_per_minute_capacity():
    from codepilot.mcp.tools.external import PerMinuteRateLimiter

    now = [120.0]
    limiter = PerMinuteRateLimiter(limit_per_minute=2, clock=lambda: now[0])

    assert limiter.check("feishu.message.send").allowed is True
    assert limiter.check("feishu.message.send").allowed is True

    denied = limiter.check("feishu.message.send")

    assert denied.allowed is False
    assert denied.error == {
        "code": "rate_limited",
        "message": "rate limit exceeded for feishu.message.send",
        "details": {
            "key": "feishu.message.send",
            "limit_per_minute": 2,
            "retry_after_seconds": 60,
        },
    }

    now[0] = 180.0

    assert limiter.check("feishu.message.send").allowed is True


def test_feishu_rate_limit_error_response_is_mcp_tool_error_payload():
    from codepilot.mcp.tools.external import (
        PerMinuteRateLimiter,
        feishu_rate_limit_error_response,
    )

    limiter = PerMinuteRateLimiter(limit_per_minute=1, clock=lambda: 10.0)
    assert limiter.check("feishu.bot.card").allowed is True
    decision = limiter.check("feishu.bot.card")

    result = feishu_rate_limit_error_response(decision)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "rate_limited"
    assert result["structuredContent"]["error"]["details"]["limit_per_minute"] == 1
