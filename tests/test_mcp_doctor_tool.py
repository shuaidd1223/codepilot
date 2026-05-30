from __future__ import annotations

from pathlib import Path
import time

import pytest

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def test_mcp_doctor_uses_in_process_checks_instead_of_cli_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    from codepilot.commands.doctor import CheckResult
    from codepilot.mcp.tools.ops import doctor as doctor_tool

    monkeypatch.setattr(
        doctor_tool,
        "invoke_cli_json",
        lambda _args: pytest.fail("doctor MCP tool should not route through CliRunner"),
        raising=False,
    )
    monkeypatch.setattr(
        doctor_tool.doctor_cmd,
        "run_all_checks",
        lambda: [CheckResult("python_version", True, "Python OK")],
    )
    monkeypatch.setattr(
        doctor_tool.doctor_cmd,
        "run_project_checks",
        lambda project_info, include_services=False: [
            CheckResult("service_daemon", True, f"services={include_services}")
        ],
    )
    monkeypatch.setattr(
        doctor_tool.doctor_cmd,
        "_dispatch_doctor_event",
        lambda *args, **kwargs: {"delivered": 0, "results": []},
    )

    result = doctor_tool.doctor("demo", services=True)

    assert result["ok"] is True
    assert result["command"] == "doctor"
    assert result["exit_code"] == 0
    assert result["data"]["project"]["name"] == "demo"
    assert result["data"]["checks"][0]["name"] == "python_version"
    assert result["data"]["checks"][1]["detail"] == "services=True"
    assert result["data"]["event_delivery"] == {"delivered": 0, "results": []}


def test_mcp_doctor_honors_services_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    from codepilot.commands.doctor import CheckResult
    from codepilot.mcp.tools.ops import doctor as doctor_tool

    include_services_values: list[bool] = []
    monkeypatch.setattr(doctor_tool.doctor_cmd, "run_all_checks", lambda: [])
    monkeypatch.setattr(
        doctor_tool.doctor_cmd,
        "run_project_checks",
        lambda project_info, include_services=False: include_services_values.append(include_services)
        or [CheckResult("project", True, f"services={include_services}")],
    )
    monkeypatch.setattr(doctor_tool.doctor_cmd, "_dispatch_doctor_event", lambda *args, **kwargs: None)

    result = doctor_tool.doctor("demo", services=False)

    assert result["ok"] is True
    assert include_services_values == [False]
    assert result["data"]["checks"][0]["detail"] == "services=False"


def test_mcp_doctor_honors_timeout_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    init_test_db(tmp_path, monkeypatch)

    from codepilot.mcp.tools.ops import doctor as doctor_tool

    monkeypatch.setattr(doctor_tool.doctor_cmd, "run_all_checks", lambda: time.sleep(2) or [])

    with pytest.raises(CodePilotToolError) as exc_info:
        doctor_tool.doctor("", timeout_seconds=1)

    assert exc_info.value.code == "doctor_timeout"
