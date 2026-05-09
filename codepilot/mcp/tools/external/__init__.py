"""Helpers shared by external-service MCP tools."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    error: dict[str, Any] | None = None


class PerMinuteRateLimiter:
    """In-memory per-key fixed-window limiter for lightweight MCP tools."""

    def __init__(
        self,
        *,
        limit_per_minute: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if limit_per_minute < 1:
            raise ValueError("limit_per_minute must be greater than or equal to 1")
        self.limit_per_minute = limit_per_minute
        self._clock = clock or time.time
        self._windows: dict[str, tuple[int, int]] = {}

    def check(self, key: str) -> RateLimitDecision:
        now = float(self._clock())
        window_start = int(now // 60) * 60
        stored_start, count = self._windows.get(key, (window_start, 0))
        if stored_start != window_start:
            stored_start = window_start
            count = 0

        if count >= self.limit_per_minute:
            retry_after = max(1, math.ceil((stored_start + 60) - now))
            return RateLimitDecision(
                allowed=False,
                error={
                    "code": "rate_limited",
                    "message": f"rate limit exceeded for {key}",
                    "details": {
                        "key": key,
                        "limit_per_minute": self.limit_per_minute,
                        "retry_after_seconds": retry_after,
                    },
                },
            )

        self._windows[key] = (stored_start, count + 1)
        return RateLimitDecision(allowed=True)


def feishu_rate_limit_error_response(decision: RateLimitDecision) -> dict[str, Any]:
    if decision.allowed or decision.error is None:
        raise ValueError("decision must be a denied rate-limit decision")
    error = decision.error
    return {
        "isError": True,
        "content": [{"type": "text", "text": str(error["message"])}],
        "structuredContent": {"error": error},
    }
