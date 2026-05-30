from __future__ import annotations

import json
from pathlib import Path

import pytest

from codepilot.mcp.server import MCPProjectContext, create_mcp_server
from codepilot.mcp.tool_registry import default_registry
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


SHELL_CONTROL_INPUTS = (
    "doctor; status",
    "doctor && status",
    "doctor | status",
    "doctor > out.txt",
    "doctor < in.txt",
)


def _init_demo_project(tmp_path: Path, monkeypatch) -> Path:
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def _ops_server(project_path: Path):
    import codepilot.mcp.tools.ops  # noqa: F401

    return create_mcp_server(
        MCPProjectContext(project_path=project_path, project="demo"),
        registry=default_registry,
        include_health=False,
        bind_sdk=False,
    )


def _error_code(payload: dict) -> str:
    return payload["structuredContent"]["error"]["code"]


def _audit_records(project_path: Path) -> list[dict]:
    path = project_path / ".codepilot" / "mcp" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_ops_exec_rejects_unregistered_commands_before_cli_invocation(
    tmp_path,
    monkeypatch,
):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    monkeypatch.setattr(
        "codepilot.mcp.tools.ops.exec.invoke_cli_json",
        lambda _args: pytest.fail("unregistered exec command reached CLI invocation"),
    )

    result = server.call_tool(
        "exec",
        {"project": "demo", "command": "task.retry"},
    )

    assert _error_code(result) == "command_not_allowed"
    assert result["structuredContent"]["error"]["details"]["command"] == "task.retry"
    records = _audit_records(project_path)
    assert records[-1]["tool"] == "exec"
    assert records[-1]["status"] == "error"
    assert records[-1]["error"]["code"] == "command_not_allowed"


@pytest.mark.parametrize("command", SHELL_CONTROL_INPUTS)
def test_ops_exec_rejects_shell_control_fragments_as_command_names(
    command,
    tmp_path,
    monkeypatch,
):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    monkeypatch.setattr(
        "codepilot.mcp.tools.ops.exec.invoke_cli_json",
        lambda _args: pytest.fail("shell-fragment exec command reached CLI invocation"),
    )

    result = server.call_tool("exec", {"project": "demo", "command": command})

    assert _error_code(result) == "command_not_allowed"
    assert result["structuredContent"]["error"]["details"]["command"] == command
    assert _audit_records(project_path)[-1]["error"]["code"] == "command_not_allowed"


def test_ops_exec_rejects_shell_control_fragments_as_extra_arguments(
    tmp_path,
    monkeypatch,
):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    monkeypatch.setattr(
        "codepilot.mcp.tools.ops.exec.invoke_cli_json",
        lambda _args: pytest.fail("exec arguments reached CLI invocation"),
    )

    result = server.call_tool(
        "exec",
        {"project": "demo", "command": "doctor", "args": ["--json; rm -rf ."]},
    )

    assert _error_code(result) == "command_args_not_allowed"
    assert _audit_records(project_path)[-1]["error"]["code"] == "command_args_not_allowed"
