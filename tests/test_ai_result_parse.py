from __future__ import annotations

import pytest

from codepilot.ai_result_parse import (
    extract_error_hint,
    parse_structured_json_output,
    unwrap_structured_payload,
)


def test_unwrap_structured_payload_prefers_structured_output():
    payload = {"structured_output": {"tasks": [{"title": "x"}]}, "tasks": []}
    assert unwrap_structured_payload(payload)["tasks"][0]["title"] == "x"


def test_unwrap_structured_payload_accepts_result_object():
    payload = {"result": {"summary": "ok", "tasks": [{"title": "x"}]}}
    assert unwrap_structured_payload(payload)["summary"] == "ok"


def test_parse_structured_json_output_rejects_non_json():
    with pytest.raises(RuntimeError, match="不是有效 JSON"):
        parse_structured_json_output("not-json", provider_name="Codex")


def test_extract_error_hint_reads_message_from_error_payload():
    raw = '{"error":{"message":"limit exceeded · resets Apr 30"}}'
    hint = extract_error_hint(raw)
    assert "limit exceeded" in hint
    assert "重置时间 Apr 30" in hint
