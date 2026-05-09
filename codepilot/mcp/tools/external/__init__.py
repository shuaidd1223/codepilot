"""Helpers shared by external-service MCP tools."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.storage import database as db


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


def invalid_arguments(message: str, **details: Any) -> CodePilotToolError:
    return CodePilotToolError(message, code="invalid_arguments", details=details)


def ensure_str(
    value: Any,
    field: str,
    *,
    required: bool = True,
    allow_empty: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise invalid_arguments(f"{field} is required", field=field)
        return None
    if not isinstance(value, str):
        raise invalid_arguments(f"{field} must be a string", field=field)
    text = value.strip()
    if required and not text:
        raise invalid_arguments(f"{field} cannot be empty", field=field)
    if not allow_empty and value != "" and not text:
        raise invalid_arguments(f"{field} cannot be blank", field=field)
    return text if not allow_empty else value


def ensure_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_arguments(f"{field} must be an integer", field=field)
    if minimum is not None and value < minimum:
        raise invalid_arguments(
            f"{field} must be greater than or equal to {minimum}",
            field=field,
            minimum=minimum,
        )
    return value


def ensure_str_list(
    value: Any,
    field: str,
    *,
    required: bool = False,
    allow_empty: bool = True,
) -> list[str] | None:
    if value is None:
        if required:
            raise invalid_arguments(f"{field} is required", field=field)
        return None
    if not isinstance(value, list):
        raise invalid_arguments(f"{field} must be a list of strings", field=field)
    result: list[str] = []
    for item in value:
        text = ensure_str(item, field, required=True)
        if text is not None:
            result.append(text)
    if not result and not allow_empty:
        raise invalid_arguments(f"{field} cannot be empty", field=field)
    return result


def resolve_project(project: Any) -> dict[str, Any]:
    project_name = ensure_str(project, "project", required=True)
    db.init_db()
    found = db.get_project(project_name or "")
    if not found:
        raise CodePilotToolError(
            f"project not found: {project_name}",
            code="project_not_found",
            details={"project": project_name},
        )
    return dict(found)


from codepilot.mcp.tools.external import feishu_notify as feishu_notify_module  # noqa: E402,F401
from codepilot.mcp.tools.external import feishu_send_to_user as feishu_send_to_user_module  # noqa: E402,F401
from codepilot.mcp.tools.external import webhook_invoke as webhook_invoke_module  # noqa: E402,F401

__all__ = [
    "PerMinuteRateLimiter",
    "RateLimitDecision",
    "feishu_rate_limit_error_response",
    "feishu_notify_module",
    "feishu_send_to_user_module",
    "webhook_invoke_module",
]
