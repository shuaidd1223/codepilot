from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import ToolRegistry, register_tool


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_mcp_audit_writes_complete_jsonl_records_and_appends(tmp_path: Path):
    from codepilot.mcp.audit import record_mcp_tool_call

    fixed_time = datetime(2026, 5, 9, 1, 2, 3, tzinfo=timezone.utc)

    first_ok = record_mcp_tool_call(
        project_path=tmp_path,
        tool_name="codepilot.ops.status",
        arguments={"project": "demo", "access_token": "secret-value"},
        status="ok",
        error=None,
        now=lambda: fixed_time,
    )
    second_ok = record_mcp_tool_call(
        project_path=tmp_path,
        tool_name="feishu.message.send",
        arguments={"chat_id": "oc_xxx", "content": "hello"},
        status="error",
        error={"code": "rate_limited", "message": "too many requests"},
        now=lambda: fixed_time,
    )

    records = _read_jsonl(tmp_path / ".codepilot" / "mcp" / "audit.jsonl")

    assert first_ok is True
    assert second_ok is True
    assert len(records) == 2
    assert records[0] == {
        "timestamp": "2026-05-09T01:02:03+00:00",
        "tool": "codepilot.ops.status",
        "arguments_summary": {"project": "demo", "access_token": "[redacted]"},
        "status": "ok",
        "error": None,
    }
    assert records[1]["tool"] == "feishu.message.send"
    assert records[1]["status"] == "error"
    assert records[1]["error"] == {"code": "rate_limited", "message": "too many requests"}


def test_mcp_audit_write_failure_is_reported_without_raising(
    tmp_path: Path,
    monkeypatch,
):
    from codepilot.mcp.audit import record_mcp_tool_call

    def fail_open(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", fail_open)

    assert (
        record_mcp_tool_call(
            project_path=tmp_path,
            tool_name="codepilot.ops.status",
            arguments={},
            status="ok",
            error=None,
        )
        is False
    )


def test_mcp_server_audits_success_and_tool_error_calls(tmp_path: Path):
    from codepilot.mcp.server import MCPProjectContext, create_mcp_server

    registry = ToolRegistry()

    @register_tool(name="demo.ok", registry=registry)
    def ok(project: str) -> dict[str, str]:
        return {"project": project}

    @register_tool(name="demo.fail", registry=registry)
    def fail(project: str) -> dict[str, str]:
        raise CodePilotToolError(
            "project missing",
            code="project_missing",
            details={"project": project},
        )

    server = create_mcp_server(
        MCPProjectContext(project_path=tmp_path, project="demo"),
        registry=registry,
        include_health=False,
        bind_sdk=False,
    )

    assert server.call_tool("demo.ok", {"project": "demo"}) == {"project": "demo"}
    error = server.call_tool("demo.fail", {"project": "missing"})

    records = _read_jsonl(tmp_path / ".codepilot" / "mcp" / "audit.jsonl")

    assert error["isError"] is True
    assert [record["tool"] for record in records] == ["demo.ok", "demo.fail"]
    assert records[0]["arguments_summary"] == {"project": "demo"}
    assert records[0]["status"] == "ok"
    assert records[0]["error"] is None
    assert records[1]["status"] == "error"
    assert records[1]["error"]["code"] == "project_missing"
    assert records[1]["error"]["message"] == "project missing"
