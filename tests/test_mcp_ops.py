from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codepilot.mcp.server import MCPProjectContext, create_mcp_server
from codepilot.mcp.tool_registry import default_registry
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


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


def _tool_by_name(name: str) -> dict[str, Any]:
    import codepilot.mcp.tools.ops  # noqa: F401

    return default_registry.get(name).schema


def _error_code(payload: dict[str, Any]) -> str:
    return payload["structuredContent"]["error"]["code"]


def _audit_records(project_path: Path) -> list[dict[str, Any]]:
    path = project_path / ".codepilot" / "mcp" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_ops_exec_rejects_arbitrary_shell_commands_and_audits_failure(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    result = server.call_tool(
        "exec",
        {
            "project": "demo",
            "command": "powershell",
            "args": ["-Command", "Remove-Item -Recurse ."],
        },
    )

    assert _error_code(result) == "command_not_allowed"
    records = _audit_records(project_path)
    assert records[-1]["tool"] == "exec"
    assert records[-1]["status"] == "error"
    assert records[-1]["error"]["code"] == "command_not_allowed"
    assert records[-1]["arguments_summary"]["command"] == "powershell"


def test_ops_tools_register_independent_contracts():
    import codepilot.mcp.tools.ops  # noqa: F401

    expected = {"doctor", "run_once", "daemon_status", "build_fix", "exec"}
    names = {tool.name for tool in default_registry.list()}

    assert expected <= names
    for name in expected:
        schema = _tool_by_name(name)
        assert schema["description"]
        assert schema["inputSchema"]["type"] == "object"
        assert schema["inputSchema"]["additionalProperties"] is False
        assert callable(default_registry.get(name).func)


def test_ops_tool_input_schemas_capture_required_fields():
    assert _tool_by_name("doctor")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("run_once")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("daemon_status")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("build_fix")["inputSchema"]["required"] == ["project"]
    assert _tool_by_name("exec")["inputSchema"]["required"] == ["project", "command"]

    exec_props = _tool_by_name("exec")["inputSchema"]["properties"]
    assert exec_props["command"] == {"type": "string"}
    assert exec_props["args"] == {
        "anyOf": [
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ],
        "default": None,
    }


def test_doctor_ops_tool_reuses_cli_json_path_and_audits_success(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    from codepilot.commands import doctor as doctor_cmd

    monkeypatch.setattr(
        doctor_cmd,
        "run_all_checks",
        lambda: [doctor_cmd.CheckResult("python_version", True, "Python ok")],
    )
    monkeypatch.setattr(doctor_cmd, "run_project_checks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(doctor_cmd, "_dispatch_doctor_event", lambda *_args, **_kwargs: None)

    result = server.call_tool("doctor", {"project": "demo", "services": True})

    assert result["command"] == "doctor"
    assert result["exit_code"] == 0
    assert result["data"]["checks"][0]["name"] == "python_version"
    records = _audit_records(project_path)
    assert records[-1]["tool"] == "doctor"
    assert records[-1]["status"] == "ok"


def test_run_once_ops_tool_reuses_run_backlog_api_and_audits_success(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)
    captured: dict[str, Any] = {}

    def fake_run_backlog(project: str, **kwargs):
        captured.update({"project": project, **kwargs})
        return {"processed": 1, "done": 1, "failed": 0, "requeued": 0, "cancelled": 0}

    monkeypatch.setattr("codepilot.commands.run.run_backlog", fake_run_backlog)

    result = server.call_tool(
        "run_once",
        {
            "project": "demo",
            "limit": 2,
            "dry_run": True,
            "executor": "builtin",
            "auto_commit": False,
        },
    )

    assert result["stats"]["processed"] == 1
    assert captured == {
        "project": "demo",
        "once": True,
        "limit": 2,
        "dry_run": True,
        "cleanup": True,
        "shell": "auto",
        "executor": "builtin",
        "auto_commit": False,
    }
    assert _audit_records(project_path)[-1]["status"] == "ok"


def test_daemon_status_ops_tool_reuses_service_status_api(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    monkeypatch.setattr(
        "codepilot.commands.daemon.daemon_service_status",
        lambda project: {
            "running": True,
            "stopping": False,
            "pid": 7654,
            "project": project,
            "started_at": "2026-05-09T10:00:00",
            "log": "daemon.log",
        },
    )

    result = server.call_tool("daemon_status", {"project": "demo"})

    assert result["status"]["running"] is True
    assert result["status"]["pid"] == 7654
    assert result["status"]["project"] == "demo"
    records = _audit_records(project_path)
    assert records[-1]["tool"] == "daemon_status"
    assert records[-1]["status"] == "ok"


def test_build_fix_ops_tool_reuses_build_fix_api_and_handles_business_errors(
    tmp_path,
    monkeypatch,
):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    def fake_run_build_fix(project: str, **kwargs):
        assert project == "demo"
        assert kwargs["task_id"] == 42
        assert kwargs["dry_run"] is True
        return {"project": project, "task_id": 42, "verdict": "dry_run", "actions": []}

    monkeypatch.setattr("codepilot.commands.build_fix.run_build_fix", fake_run_build_fix)

    result = server.call_tool(
        "build_fix",
        {"project": "demo", "task_id": 42, "dry_run": True},
    )

    assert result["task_id"] == 42
    assert result["verdict"] == "dry_run"
    records = _audit_records(project_path)
    assert records[-1]["tool"] == "build_fix"
    assert records[-1]["status"] == "ok"

    from codepilot.commands.build_fix import BuildFixError

    def fail_run_build_fix(*_args, **_kwargs):
        raise BuildFixError("项目 demo 没有 failed 任务可修复")

    monkeypatch.setattr("codepilot.commands.build_fix.run_build_fix", fail_run_build_fix)

    error = server.call_tool("build_fix", {"project": "demo"})

    assert _error_code(error) == "build_fix_error"
    records = _audit_records(project_path)
    assert records[-1]["tool"] == "build_fix"
    assert records[-1]["status"] == "error"


def test_ops_exec_allows_whitelisted_codepilot_command_via_cli_reflection(
    tmp_path,
    monkeypatch,
):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    from codepilot.commands import doctor as doctor_cmd

    monkeypatch.setattr(
        doctor_cmd,
        "run_all_checks",
        lambda: [doctor_cmd.CheckResult("python_version", True, "Python ok")],
    )
    monkeypatch.setattr(doctor_cmd, "run_project_checks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(doctor_cmd, "_dispatch_doctor_event", lambda *_args, **_kwargs: None)

    result = server.call_tool("exec", {"project": "demo", "command": "doctor"})

    assert result["command"] == "doctor"
    assert result["exit_code"] == 0
    assert result["data"]["checks"][0]["name"] == "python_version"
    records = _audit_records(project_path)
    assert records[-1]["tool"] == "exec"
    assert records[-1]["status"] == "ok"


def test_ops_tools_return_structured_errors_for_bad_arguments(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _ops_server(project_path)

    missing_project = server.call_tool("doctor", {})
    bad_limit = server.call_tool("run_once", {"project": "demo", "limit": 0})
    bad_executor = server.call_tool("run_once", {"project": "demo", "executor": "shell"})
    missing_registered_project = server.call_tool("daemon_status", {"project": "missing"})
    exec_bad_args = server.call_tool("exec", {"project": "demo", "command": "doctor", "args": ["--fix"]})

    assert _error_code(missing_project) == "invalid_arguments"
    assert _error_code(bad_limit) == "invalid_arguments"
    assert _error_code(bad_executor) == "invalid_executor"
    assert _error_code(missing_registered_project) == "project_not_found"
    assert _error_code(exec_bad_args) == "command_args_not_allowed"
