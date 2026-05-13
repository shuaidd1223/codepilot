"""Operational-service MCP tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from click.testing import CliRunner

from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tools._helpers import (  # noqa: F401
    ensure_bool,
    ensure_int,
    ensure_str,
    ensure_str_list,
    invalid_arguments,
    resolve_project,
)
from codepilot.storage import database as db


VALID_EXECUTORS = {"auto", "dispatch", "builtin"}
VALID_SHELLS = {"auto", "pwsh", "powershell", "bash", "zsh"}


@dataclass(frozen=True)
class WhitelistedCommand:
    key: str
    cli_args: tuple[str, ...]
    allow_user_args: bool = False


ALLOWED_EXEC_COMMANDS: dict[str, WhitelistedCommand] = {
    "doctor": WhitelistedCommand("doctor", ("doctor",)),
    "status": WhitelistedCommand("status", ("status",)),
    "event.schema": WhitelistedCommand("event.schema", ("event", "schema")),
    "hook.validate": WhitelistedCommand("hook.validate", ("hook", "validate")),
    "skill.list": WhitelistedCommand("skill.list", ("skill", "list")),
}


def ensure_executor(value: Any, field: str = "executor") -> str:
    executor = (ensure_str(value, field, required=True) or "").lower()
    if executor not in VALID_EXECUTORS:
        raise CodePilotToolError(
            f"{field} has invalid value: {value}",
            code="invalid_executor",
            details={"field": field, "value": value, "allowed": sorted(VALID_EXECUTORS)},
        )
    return executor


def ensure_shell(value: Any, field: str = "shell") -> str:
    shell = (ensure_str(value, field, required=True) or "").lower()
    if shell not in VALID_SHELLS:
        raise CodePilotToolError(
            f"{field} has invalid value: {value}",
            code="invalid_shell",
            details={"field": field, "value": value, "allowed": sorted(VALID_SHELLS)},
        )
    return shell


def invoke_cli_json(args: list[str], timeout: int | None = None) -> dict[str, Any]:
    from codepilot.cli import main

    def _run() -> Any:
        return CliRunner().invoke(main, args)

    if timeout is not None:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run)
            try:
                result = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                raise CodePilotToolError(
                    "CLI command timed out",
                    code="cli_timeout",
                    details={"args": args, "timeout": timeout},
                )
    else:
        result = _run()

    output = (result.output or "").strip()
    try:
        payload = json.loads(output) if output else {}
    except json.JSONDecodeError as exc:
        raise CodePilotToolError(
            "CodePilot CLI did not return JSON",
            code="cli_non_json_output",
            details={
                "args": args,
                "exit_code": result.exit_code,
                "output_tail": output[-1000:],
            },
        ) from exc

    if result.exit_code != 0:
        error = payload.get("error") if isinstance(payload, dict) else None
        raise CodePilotToolError(
            str((error or {}).get("message") or output or f"CLI failed: {args[0]}"),
            code=str((error or {}).get("code") or "ops_command_failed"),
            details={
                "args": args,
                "exit_code": result.exit_code,
                "data": payload.get("data") if isinstance(payload, dict) else None,
            },
        )

    if not isinstance(payload, dict):
        raise CodePilotToolError(
            "CodePilot CLI returned invalid JSON payload",
            code="cli_invalid_json_payload",
            details={"args": args, "exit_code": result.exit_code},
        )
    payload["exit_code"] = result.exit_code
    return payload


def project_cli_args(project: str) -> list[str]:
    return ["-p", project]


from codepilot.mcp.tools.ops import build_fix as build_fix_module  # noqa: E402,F401
from codepilot.mcp.tools.ops import daemon_status as daemon_status_module  # noqa: E402,F401
from codepilot.mcp.tools.ops import doctor as doctor_module  # noqa: E402,F401
from codepilot.mcp.tools.ops import exec as exec_module  # noqa: E402,F401
from codepilot.mcp.tools.ops import run_once as run_once_module  # noqa: E402,F401


__all__ = [
    "ALLOWED_EXEC_COMMANDS",
    "WhitelistedCommand",
    "build_fix_module",
    "daemon_status_module",
    "doctor_module",
    "exec_module",
    "run_once_module",
]
