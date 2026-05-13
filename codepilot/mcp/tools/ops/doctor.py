from __future__ import annotations

import concurrent.futures
from typing import Any

import click

from codepilot.commands import doctor as doctor_cmd
from codepilot.mcp.protocol import CodePilotToolError
from codepilot.mcp.tool_registry import register_tool
from codepilot.mcp.tools._helpers import ensure_bool, ensure_int


@register_tool(description="为已注册项目运行 CodePilot doctor 检查。")
def doctor(
    project: str,
    services: bool = False,
    fix: bool = False,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """在当前进程内运行 doctor 检查，避免 MCP 工具再嵌套 CliRunner。"""
    timeout = ensure_int(timeout_seconds, "timeout_seconds", minimum=1)
    include_services = ensure_bool(services, "services")
    fix_mode = ensure_bool(fix, "fix")

    try:
        fix_result, results, project_info, event_delivery = _run_doctor_with_timeout(
            project,
            services=include_services,
            fix=fix_mode,
            timeout_seconds=timeout,
        )
    except click.ClickException as exc:
        raise CodePilotToolError(
            exc.format_message(),
            code="doctor_failed",
            details={"project": project},
        ) from exc
    except CodePilotToolError:
        raise

    overall_ok = not any(item.severity == "error" for item in results)
    data: dict[str, Any] = {
        "checks": [item.to_dict() for item in results],
        "status_emoji": doctor_cmd._summary_status_emoji(results),
    }
    if event_delivery is not None:
        data["event_delivery"] = event_delivery
    if fix_result is not None:
        data["fix"] = fix_result
    if project_info:
        data["project"] = {
            "name": project_info.get("name"),
            "path": project_info.get("path"),
        }
    return {
        "ok": overall_ok,
        "command": "doctor",
        "exit_code": 0,
        "data": data,
    }


def _run_doctor_with_timeout(
    project: str,
    *,
    services: bool,
    fix: bool,
    timeout_seconds: int,
) -> tuple[Any, list[Any], dict[str, Any] | None, dict[str, Any] | None]:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run_doctor_checks, project, services=services, fix=fix)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        raise CodePilotToolError(
            f"doctor timed out after {timeout_seconds}s",
            code="doctor_timeout",
            details={"project": project, "timeout_seconds": timeout_seconds},
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _run_doctor_checks(
    project: str,
    *,
    services: bool,
    fix: bool,
) -> tuple[Any, list[Any], dict[str, Any] | None, dict[str, Any] | None]:
    fix_result = doctor_cmd._run_setup_fix(project) if fix else None
    results = doctor_cmd.run_all_checks()
    project_info = doctor_cmd._resolve_project(project) if (project or services or fix) else None
    if project or services or fix:
        results.extend(doctor_cmd.run_project_checks(project_info, include_services=services))
    overall_ok = not any(item.severity == "error" for item in results)
    event_delivery = doctor_cmd._dispatch_doctor_event(
        project_info,
        overall_ok=overall_ok,
        results=results,
        fix_result=fix_result,
    )
    return fix_result, results, project_info, event_delivery
