"""Protocol-level data structures for CodePilot MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class CodePilotToolError(Exception):
    """Exception type that can be rendered as an MCP tool error response."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_error",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = dict(details or {})

    def to_tool_response(self) -> dict[str, Any]:
        return {
            "isError": True,
            "content": [{"type": "text", "text": self.message}],
            "structuredContent": {
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "details": self.details,
                }
            },
        }


@dataclass(frozen=True)
class ProgressEvent:
    """Normalized event yielded by long-running MCP tools."""

    message: str
    progress: int | float | None = None
    total: int | float | None = None
    data: Mapping[str, Any] | None = None
    type: str = "progress"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "message": self.message}
        if self.progress is not None:
            payload["progress"] = self.progress
        if self.total is not None:
            payload["total"] = self.total
        if self.data is not None:
            payload["data"] = dict(self.data)
        return payload


def normalize_progress_event(value: ProgressEvent | Mapping[str, Any] | str) -> ProgressEvent:
    if isinstance(value, ProgressEvent):
        return value
    if isinstance(value, str):
        return ProgressEvent(message=value)
    if isinstance(value, Mapping):
        message = value.get("message")
        if not isinstance(message, str) or not message:
            raise TypeError("progress event mapping requires a non-empty string message")
        return ProgressEvent(
            message=message,
            progress=value.get("progress"),
            total=value.get("total"),
            data=value.get("data"),
        )
    raise TypeError(f"unsupported progress event value: {type(value).__name__}")
