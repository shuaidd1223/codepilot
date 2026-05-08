"""Project-local self-update dry-run planning command."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.output import echo
from codepilot.storage import database as db


SUPPORTED_PROVIDERS = ("codex", "claude", "opencode", "gemini", "custom")


class SelfUpdateError(ValueError):
    """Raised when self-update cannot continue safely."""


def _resolve_project(project: str | None) -> dict:
    db.init_db()
    if project:
        found = db.get_project(project)
        if not found:
            raise SelfUpdateError(f"项目 '{project}' 未注册。")
        return found
    found = db.find_project_by_path(Path.cwd())
    if not found:
        raise SelfUpdateError("当前目录不属于已注册项目；请使用 -p/--project 指定项目。")
    return found


def _git_status(project_root: Path) -> dict[str, Any]:
    if not (project_root / ".git").exists():
        return {"available": False, "clean": None, "summary": "项目目录不是 Git 工作树。", "changes": []}
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "clean": None, "summary": str(exc), "changes": []}
    changes = [line for line in completed.stdout.splitlines() if line.strip()]
    return {
        "available": completed.returncode == 0,
        "clean": completed.returncode == 0 and not changes,
        "summary": "工作区干净" if completed.returncode == 0 and not changes else f"工作区有 {len(changes)} 个变更",
        "changes": changes[:50],
    }


def _collect_preflight(project_info: dict, providers: tuple[str, ...]) -> dict[str, Any]:
    from codepilot.commands import doctor as doctor_cmd
    from codepilot.commands.exec_cmd import run_exec
    from codepilot.core import event_plugins, hook_registry

    project_root = Path(project_info["path"]).resolve()
    doctor_checks = doctor_cmd.run_project_checks(project_info, include_services=False)
    provider_results = []
    for provider in providers:
        command = (provider, "--version") if provider != "custom" else ()
        provider_results.append(
            run_exec(
                project_info,
                provider=provider,
                command_parts=command,
                cwd=None,
                dry_run=True,
                timeout_seconds=30,
                tail_lines=20,
            )
        )
    return {
        "doctor": {
            "ok": not any(item.severity == "error" for item in doctor_checks),
            "checks": [item.to_dict() for item in doctor_checks],
        },
        "hook": hook_registry.validate_hooks(project_root),
        "event_schema": {
            "schema_version": event_plugins.SCHEMA_VERSION,
            "count": len(event_plugins.event_schemas()),
            "types": [item["type"] for item in event_plugins.event_schemas()],
        },
        "exec": provider_results,
        "git": _git_status(project_root),
    }


def _failed_task_evidence(project_name: str, *, limit: int = 5) -> list[dict[str, Any]]:
    failed = db.list_tasks(project=project_name, status="failed")[: max(1, int(limit or 1))]
    results = []
    for task in failed:
        results.append(
            {
                "id": task["id"],
                "title": task.get("title") or "",
                "agent": task.get("agent") or "",
                "status": task.get("status") or "",
                "phase": task.get("run_phase") or "",
                "error": task.get("error_message") or "",
            }
        )
    return results


def _collect_evidence(project_info: dict, goal: str) -> dict[str, Any]:
    from codepilot.commands.explore import explore_project
    from codepilot.commands.trace import collect_trace_events
    from codepilot.commands.wiki import wiki_context

    trace_events = collect_trace_events(project_info, limit=20)
    explore = explore_project(goal, project=project_info["name"], use_wiki=True)
    wiki = wiki_context(project_info, goal, enabled=True, limit=5)
    return {
        "trace": {"count": len(trace_events), "events": trace_events},
        "explore": explore,
        "failed_tasks": _failed_task_evidence(project_info["name"]),
        "wiki_context": wiki,
    }


def _build_next_actions(project_name: str, goal: str, failed_tasks: list[dict[str, Any]]) -> list[dict[str, str]]:
    actions = [
        {
            "label": "生成可审查计划 artifact",
            "command": f'codepilot plan -p {project_name} --use-wiki "{goal}" --json',
        },
        {
            "label": "创建但不执行自我迭代任务",
            "command": f'codepilot go -p {project_name} --no-execute "{goal}"',
        },
        {
            "label": "运行 focused self-update 测试",
            "command": "pytest -q tests/test_self_update_command.py",
        },
    ]
    if failed_tasks:
        actions.append(
            {
                "label": " dry-run 失败任务修复闭环",
                "command": f"codepilot build-fix -p {project_name} --task-id {failed_tasks[0]['id']} --dry-run --json",
            }
        )
    return actions


def run_self_update(
    project_info: dict,
    *,
    goal: str,
    providers: tuple[str, ...],
    dry_run: bool,
) -> dict[str, Any]:
    if not dry_run:
        raise SelfUpdateError("self-update v1 只支持 --dry-run；不会自动修改代码、创建任务或提交。")
    normalized_goal = " ".join(str(goal or "").split())
    if not normalized_goal:
        raise SelfUpdateError("self-update 需要提供改进目标。")
    normalized_providers = tuple(dict.fromkeys(str(item).lower() for item in (providers or ("codex",))))
    invalid = [item for item in normalized_providers if item not in SUPPORTED_PROVIDERS]
    if invalid:
        raise SelfUpdateError(f"不支持的 provider：{', '.join(invalid)}")

    from codepilot.commands.plan import build_execution_plan

    evidence = _collect_evidence(project_info, normalized_goal)
    plan_payload = build_execution_plan(
        normalized_goal,
        source="self-update",
        wiki_context=evidence["wiki_context"],
    )
    return {
        "project": {"name": project_info["name"], "path": project_info["path"]},
        "goal": normalized_goal,
        "preflight": _collect_preflight(project_info, normalized_providers),
        "evidence": evidence,
        "plan": {
            "summary": plan_payload["summary"],
            "files": plan_payload["files"],
            "task_candidates": plan_payload["task_candidates"],
            "risks": plan_payload["risks"],
            "wiki_context": plan_payload["wiki_context"],
        },
        "verification": plan_payload["verification_plan"],
        "next_actions": _build_next_actions(project_info["name"], normalized_goal, evidence["failed_tasks"]),
        "verdict": "dry_run",
    }


@click.command("self-update")
@click.argument("goal_parts", nargs=-1, required=True)
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--provider", "providers", multiple=True, type=click.Choice(SUPPORTED_PROVIDERS, case_sensitive=False), help="要预检的 provider，可重复；默认 codex")
@click.option("--dry-run", is_flag=True, help="只做预检、证据采集和内存计划，不修改项目")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def self_update(
    ctx: click.Context,
    goal_parts: tuple[str, ...],
    project: str | None,
    providers: tuple[str, ...],
    dry_run: bool,
    json_mode: bool,
) -> None:
    """项目内自我迭代 dry-run：预检、收集证据并生成下一步计划。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        data = run_self_update(
            project_info,
            goal=" ".join(goal_parts).strip(),
            providers=providers or ("codex",),
            dry_run=dry_run,
        )
    except (SelfUpdateError, click.ClickException) as exc:
        if json_mode:
            emit_json_payload("self-update", ok=False, data={}, error=str(exc), error_code="self_update_error")
            ctx.exit(1)
        raise click.ClickException(str(exc)) from exc
    if json_mode:
        emit_json_payload("self-update", ok=True, data=data)
        return
    echo(f"[green]self-update dry-run 完成[/green]  project={data['project']['name']}")
    echo(f"  goal: {data['goal']}")
    echo(f"  next: {data['next_actions'][0]['command']}")
