"""Shared parsing helpers for AI raw outputs and planner envelopes."""

from __future__ import annotations

import json


def unwrap_structured_payload(payload: dict) -> dict:
    """Unwrap common provider envelopes and return the structured body."""
    if not isinstance(payload, dict):
        return {}
    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload


def parse_structured_json_output(raw: str, *, provider_name: str) -> dict:
    """Parse one provider response that must contain an object payload."""
    text = (raw or "").strip()
    if not text:
        raise RuntimeError(f"{provider_name} 没有返回任务拆分结果，暂时无法继续自动规划。")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{provider_name} 返回的任务拆分结果不是有效 JSON，暂时无法继续自动规划。") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"{provider_name} 返回的任务拆分结果格式不正确，暂时无法继续自动规划。")

    normalized = unwrap_structured_payload(payload)
    if not isinstance(normalized, dict):
        raise RuntimeError(f"{provider_name} 返回的任务拆分结果格式不正确，暂时无法继续自动规划。")
    return normalized


def extract_error_hint(raw: str) -> str:
    """Condense stderr / exception text into one readable line."""
    text = (raw or "").strip()
    if not text:
        return ""

    def _from_payload(payload: dict) -> str:
        value = payload.get("result") or payload.get("message")
        if isinstance(payload.get("error"), dict):
            value = payload["error"].get("message") or value
        elif isinstance(payload.get("error"), str):
            value = payload.get("error") or value
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                extracted = _from_payload(payload)
                if extracted:
                    text = extracted
                    break
        else:
            text = line
            break

    text = text.replace("You've hit your limit", "当前账号额度已用完")
    text = text.replace("resets", "重置时间")
    text = text.replace("·", "，")
    return text[:220]
