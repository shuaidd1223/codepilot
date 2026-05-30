"""Natural-language requirement planning and execution.

This module owns :func:`run_requirement_workflow` and its helpers. It is imported
and re-exported by :mod:`codepilot.commands.auto` so that the historical import
path ``from codepilot.commands.auto import run_requirement_workflow`` keeps working.

Several dependencies (AI planner, backlog runner, dashboard renderer) are looked
up at call time via ``codepilot.commands.auto`` so test monkeypatches on the
shell module (e.g. ``monkeypatch.setattr(auto_cmd, "generate_task_breakdown", ...)``)
take effect inside this workflow.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click

from codepilot.storage import database as db  # noqa: F401 (re-export for tests)
from codepilot.ai_support.interaction_controller import build_workflow_session_record
from codepilot.commands import auto_project_resolution as _project_resolution
from codepilot.commands import auto_workflow_planning as _planning_flow
from codepilot.core.config import (
    find_global_config,
    load_project_config,
    resolve_project_config_reference,
)

TEMP_SESSION_NAME = _project_resolution.TEMP_SESSION_NAME


def _normalize_agent_name(value: str) -> str:
    from codepilot.ai_support.service import normalize_agent_name

    return normalize_agent_name(value)


def _build_task_markdown_from_plan(item: dict, *, language: str = "en") -> str:
    from codepilot.ai_support.service import build_task_markdown_from_plan

    return build_task_markdown_from_plan(item, language=language)


def _shell():
    """Return the ``codepilot.commands.auto`` module for dynamic attribute lookup."""
    return sys.modules["codepilot.commands.auto"]


def _should_fallback_codex_planning(exc: Exception) -> bool:
    return _planning_flow.should_fallback_codex_planning(exc)


def _is_claude_family_planner(planner: str) -> bool:
    return _planning_flow.is_claude_family_planner(
        planner,
        normalize_agent_name=_normalize_agent_name,
    )


def _generate_task_breakdown_via_shell(shell, **kwargs):
    return _planning_flow.generate_task_breakdown_via_shell(shell, **kwargs)


def normalize_requirement_text(text: str) -> str:
    """Normalize a free-text requirement into a planner-friendly single line."""
    return " ".join(str(text or "").split())


def resolve_project_for_prompt(
    project: Optional[str] = None,
    cwd: Optional[Path] = None,
    *,
    auto_register: bool = True,
    allow_temporary: bool = False,
    require_registered: bool = False,
) -> dict:
    """Resolve project via the dedicated project-resolution module."""
    return _project_resolution.resolve_project_for_prompt(
        project=project,
        cwd=cwd,
        auto_register=auto_register,
        allow_temporary=allow_temporary,
        require_registered=require_registered,
    )


def _project_config(project_info: dict):
    return load_project_config(project_info)


def _provider_context(project_info: dict) -> str:
    """Prefer an explicitly stored AGENTS.toml path when resolving CLI providers."""
    return str(resolve_project_config_reference(project_info) or project_info["path"])


def _has_explicit_automation_task_agent(project_info: dict, cfg=None) -> bool:
    config_ref = project_info.get("config_file")
    if not config_ref:
        return bool(
            cfg
            and getattr(getattr(cfg, "automation", None), "task_agent", "")
            and getattr(cfg.automation, "task_agent", "") != "dual"
        )
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        with open(config_ref, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return False
    automation = data.get("automation", {}) if isinstance(data, dict) else {}
    return isinstance(automation, dict) and "task_agent" in automation


def _should_execute(project_info: dict, execute: Optional[bool]) -> bool:
    if execute is not None:
        return execute

    cfg = _shell()._project_config(project_info)
    if cfg:
        if cfg.automation.confirm_before_execute and click.get_text_stream("stdin").isatty():
            return click.confirm("规划完成，是否立即开始执行？", default=cfg.automation.auto_execute)
        return cfg.automation.auto_execute
    return True


def _resolve_effective_options(
    project_info: dict,
    *,
    planner: Optional[str] = None,
    executor: Optional[str] = None,
    auto_commit: Optional[bool] = None,
    max_tasks: int = 0,
    max_retries: int = 0,
) -> dict:
    cfg = _shell()._project_config(project_info)
    return {
        "planner": planner or (cfg.automation.planner if cfg else "codex"),
        "executor": executor or (cfg.automation.executor if cfg else "builtin"),
        "auto_commit": (cfg.automation.auto_commit if auto_commit is None and cfg else (True if auto_commit is None else auto_commit)),
        "max_tasks": max_tasks or (cfg.automation.max_tasks if cfg else 5),
        "max_retries": max_retries or (cfg.automation.max_retries if cfg else 3),
        "confirm_before_execute": cfg.automation.confirm_before_execute if cfg else False,
        "auto_execute": cfg.automation.auto_execute if cfg else True,
    }


def _resolve_task_agent(project_info: dict, agent: Optional[str], executor: str) -> str:
    """Resolve task agent and fail early with a human-readable message when unavailable."""
    shell = _shell()
    cfg = shell._project_config(project_info)
    default_agent = (
        cfg.automation.task_agent
        if cfg
        and cfg.automation
        and getattr(cfg.automation, "task_agent", "")
        and shell._has_explicit_automation_task_agent(project_info, cfg)
        else cfg.project.default_mode
        if cfg and cfg.project and cfg.project.default_mode
        else project_info.get("default_mode") or "dual"
    )
    raw_agent = (agent or default_agent).strip()
    normalized = _normalize_agent_name(raw_agent)

    provider_context = _provider_context(project_info)

    if normalized == "dual":
        if executor == "builtin":
            available, message = shell.check_provider_availability("dual", project_path=provider_context)
            if not available:
                raise click.ClickException(message)
        return "dual"

    available, message = shell.check_provider_availability(normalized, project_path=provider_context)
    if not available:
        raise click.ClickException(message)

    if executor == "builtin" and normalized not in {
        "codex",
        "claude",
        "claude-node",
        "claude-sonnet",
        "claude-opus",
        "claude-haiku",
        "opencode",
    }:
        raise click.ClickException(
            f"内置执行器暂时不支持 `{raw_agent}`。请改用 codex、claude、claude-node、opencode 或 dual。"
        )

    return normalized


def _fallback_single_task_breakdown(*, title: str, priority: str, exc: Exception) -> dict:
    return _planning_flow.fallback_single_task_breakdown(
        title=title,
        priority=priority,
        exc=exc,
    )


def _normalize_task_spec(item: dict) -> dict:
    return _planning_flow.normalize_task_spec(item)


def _resolve_planning_mode(project_info: dict) -> bool:
    cfg = _project_config(project_info)
    return bool(not cfg or getattr(cfg.automation, "two_stage_planning", True))


def _agent_language_for_project(project_path: str) -> str:
    cfg = load_project_config(project_path)
    if cfg and getattr(cfg, "automation", None):
        cfg_path_text = getattr(cfg, "config_file_path", "") or ""
        cfg_path = Path(cfg_path_text) if cfg_path_text else None
        global_path = find_global_config()
        if cfg_path and not (global_path and cfg_path.resolve() == global_path.resolve()):
            return str(getattr(cfg.automation, "agent_language", "en") or "en")

    cwd_cfg = load_project_config(Path.cwd())
    if cwd_cfg and getattr(cwd_cfg, "automation", None):
        cwd_cfg_path_text = getattr(cwd_cfg, "config_file_path", "") or ""
        cwd_cfg_path = Path(cwd_cfg_path_text) if cwd_cfg_path_text else None
        global_path = find_global_config()
        if cwd_cfg_path and not (global_path and cwd_cfg_path.resolve() == global_path.resolve()):
            return str(getattr(cwd_cfg.automation, "agent_language", "en") or "en")

    if cfg and getattr(cfg, "automation", None):
        return str(getattr(cfg.automation, "agent_language", "en") or "en")
    return "en"


def _list_existing_open_tasks(project_name: str) -> list[dict]:
    return _planning_flow.list_existing_open_tasks(project_name)


def _evaluate_planning_quality(title: str, breakdown: dict) -> tuple[list[str], list[str]]:
    return _planning_flow.evaluate_planning_quality(title, breakdown)


def _render_quality_feedback(blocking: list[str], advisory: list[str], *, limit: int = 8) -> str:
    return _planning_flow.render_quality_feedback(blocking, advisory, limit=limit)


def _plan_requirement_breakdown(
    *,
    shell,
    title: str,
    planner: str,
    priority: str,
    max_tasks: int,
    project_name: str,
    project_path: str,
    project_info: dict,
    two_stage_enabled: bool,
):
    return _planning_flow.plan_requirement_breakdown(
        shell=shell,
        title=title,
        planner=planner,
        priority=priority,
        max_tasks=max_tasks,
        project_name=project_name,
        project_path=project_path,
        config_ref=_provider_context(project_info),
        two_stage_enabled=two_stage_enabled,
        normalize_agent_name=_normalize_agent_name,
        should_fallback_codex_planning_fn=_should_fallback_codex_planning,
    )


def _echo_dedup_skips(dedup_skipped: list[dict]) -> None:
    _planning_flow.echo_dedup_skips(dedup_skipped)


def _derive_breakdown_meta(breakdown: dict) -> tuple[str, bool]:
    return _planning_flow.derive_breakdown_meta(breakdown)


def _create_tasks_from_breakdown(
    *,
    breakdown: dict,
    project_name: str,
    project_path: str,
    task_agent: str,
    priority: str,
    max_retries: int,
    task_source: str = "user",
    work_item: dict | None = None,
) -> list[dict]:
    agent_language = _agent_language_for_project(project_path)
    return _planning_flow.create_tasks_from_breakdown(
        breakdown=breakdown,
        project_name=project_name,
        project_path=project_path,
        task_agent=task_agent,
        priority=priority,
        max_retries=max_retries,
        task_source=task_source,
        work_item=work_item,
        build_task_markdown_from_plan=lambda item: _build_task_markdown_from_plan(item, language=agent_language),
    )


def _build_requirement_payload(
    *,
    project_name: str,
    breakdown: dict,
    complexity: str,
    should_split: bool,
    task_agent: str,
    created_tasks: list[dict],
    will_execute: bool,
) -> dict:
    return {
        "project": project_name,
        "summary": breakdown.get("summary", ""),
        "complexity": complexity,
        "should_split": should_split,
        "task_agent": task_agent,
        "tasks": created_tasks,
        "will_execute": will_execute,
        "workflow_session": build_workflow_session_record(
            phase="plan",
            intent="requirement",
            next_action="execute" if will_execute else "confirm",
        ),
    }


def _emit_non_json_plan_output(
    *,
    shell,
    project_name: str,
    breakdown: dict,
    created_tasks: list[dict],
    complexity: str,
    should_split: bool,
    task_agent: str,
    quiet: bool,
) -> None:
    from codepilot.core.output import echo

    label = "复杂任务" if should_split else "简单任务"
    echo(f"[green][OK] 已识别为{label}[/green]  complexity={complexity}")
    if breakdown.get("summary"):
        click.echo(f"  摘要: {breakdown['summary']}")
    for task in created_tasks:
        dep = f" depends_on=#{json.loads(task['depends_on'])[0]}" if task.get("depends_on") else ""
        click.echo(f"  #{task['id']}  {task['title']}  [{task['priority']}]  agent={task_agent}{dep}")
    if not quiet:
        shell.render_project_dashboard(
            project_name,
            include_done=False,
            max_rows=max(8, len(created_tasks)),
            title="任务面板",
        )


def _run_requirement_backlog(
    *,
    shell,
    project_name: str,
    task_count: int,
    executor: str,
    auto_commit: bool,
    quiet: bool,
) -> dict:
    return shell.run_backlog(
        project_name,
        once=False,
        limit=task_count,
        executor=executor,
        auto_commit=auto_commit,
        retry_on_failure=False,
        quiet=quiet,
    )


@dataclass(frozen=True)
class _RequirementWorkflowRuntime:
    title: str
    project_name: str
    project_path: str
    planner: str
    executor: str
    auto_commit: bool
    max_tasks: int
    max_retries: int
    task_agent: str
    two_stage_enabled: bool


@dataclass
class _RequirementDecisionResult:
    breakdown: dict
    complexity: str
    should_split: bool
    created_tasks: list[dict]
    will_execute: bool
    payload: dict


def _resolve_requirement_runtime(
    *,
    shell,
    project_info: dict,
    title: str,
    planner: Optional[str],
    task_agent: Optional[str],
    executor: Optional[str],
    auto_commit: Optional[bool],
    max_tasks: int,
    max_retries: int,
) -> _RequirementWorkflowRuntime:
    normalized_title = normalize_requirement_text(title)
    if not normalized_title:
        raise click.ClickException("需求文本不能为空")

    project_name = project_info["name"]
    project_path = project_info["path"]
    effective = _resolve_effective_options(
        project_info,
        planner=planner,
        executor=executor,
        auto_commit=auto_commit,
        max_tasks=max_tasks,
        max_retries=max_retries,
    )
    resolved_executor = effective["executor"]
    resolved_task_agent = shell._resolve_task_agent(project_info, task_agent, resolved_executor)
    return _RequirementWorkflowRuntime(
        title=normalized_title,
        project_name=project_name,
        project_path=project_path,
        planner=effective["planner"],
        executor=resolved_executor,
        auto_commit=effective["auto_commit"],
        max_tasks=effective["max_tasks"],
        max_retries=effective["max_retries"],
        task_agent=resolved_task_agent,
        two_stage_enabled=_resolve_planning_mode(project_info),
    )


def _run_requirement_decision_phase(
    *,
    shell,
    project_info: dict,
    runtime: _RequirementWorkflowRuntime,
    priority: str,
    execute: Optional[bool],
    task_source: str = "user",
    work_item: dict | None = None,
) -> _RequirementDecisionResult:
    from codepilot.core.output import echo

    echo(f"[cyan]收到需求：{runtime.title}[/cyan]")
    breakdown = _plan_requirement_breakdown(
        shell=shell,
        title=runtime.title,
        planner=runtime.planner,
        priority=priority,
        max_tasks=runtime.max_tasks,
        project_name=runtime.project_name,
        project_path=runtime.project_path,
        project_info=project_info,
        two_stage_enabled=runtime.two_stage_enabled,
    )
    complexity, should_split = _derive_breakdown_meta(breakdown)
    _echo_dedup_skips(breakdown.get("dedup_skipped") or [])
    created_tasks = _create_tasks_from_breakdown(
        breakdown=breakdown,
        project_name=runtime.project_name,
        project_path=runtime.project_path,
        task_agent=runtime.task_agent,
        priority=priority,
        max_retries=runtime.max_retries,
        task_source=task_source,
        work_item=work_item,
    )
    will_execute = _should_execute(project_info, execute)
    payload = _build_requirement_payload(
        project_name=runtime.project_name,
        breakdown=breakdown,
        complexity=complexity,
        should_split=should_split,
        task_agent=runtime.task_agent,
        created_tasks=created_tasks,
        will_execute=will_execute,
    )
    return _RequirementDecisionResult(
        breakdown=breakdown,
        complexity=complexity,
        should_split=should_split,
        created_tasks=created_tasks,
        will_execute=will_execute,
        payload=payload,
    )


def _execute_requirement_json_phase(
    *,
    shell,
    runtime: _RequirementWorkflowRuntime,
    decision: _RequirementDecisionResult,
) -> dict:
    payload = decision.payload
    if decision.will_execute:
        payload["run"] = shell.run_backlog(
            runtime.project_name,
            once=False,
            limit=len(decision.created_tasks),
            executor=runtime.executor,
            auto_commit=runtime.auto_commit,
        )
    return payload


def _execute_requirement_plain_phase(
    *,
    shell,
    runtime: _RequirementWorkflowRuntime,
    decision: _RequirementDecisionResult,
    quiet: bool,
) -> dict:
    from codepilot.core.output import echo

    payload = decision.payload
    _emit_non_json_plan_output(
        shell=shell,
        project_name=runtime.project_name,
        breakdown=decision.breakdown,
        created_tasks=decision.created_tasks,
        complexity=decision.complexity,
        should_split=decision.should_split,
        task_agent=runtime.task_agent,
        quiet=quiet,
    )

    if not decision.will_execute:
        echo()
        echo("[dim]已完成规划，未自动执行[/dim]")
        return payload

    echo()
    echo("[cyan]开始自动执行...[/cyan]")
    stats = _run_requirement_backlog(
        shell=shell,
        project_name=runtime.project_name,
        task_count=len(decision.created_tasks),
        executor=runtime.executor,
        auto_commit=runtime.auto_commit,
        quiet=quiet,
    )
    payload["run"] = stats
    echo(
        f"\n[dim]Workflow 完成: processed={stats['processed']} done={stats['done']} "
        f"failed={stats['failed']} requeued={stats['requeued']}[/dim]"
    )
    return payload


def run_requirement_workflow(
    *,
    project_info: dict,
    title: str,
    planner: Optional[str] = None,
    task_agent: Optional[str] = None,
    priority: str = "P2",
    max_tasks: int = 0,
    execute: Optional[bool] = None,
    executor: Optional[str] = None,
    auto_commit: Optional[bool] = None,
    max_retries: int = 0,
    json_mode: bool = False,
    quiet: bool = False,
    task_source: str = "user",
    work_item: dict | None = None,
) -> dict:
    """Plan one requirement (decision phase) and optionally execute it."""
    shell = _shell()
    if project_info.get("is_temporary"):
        raise click.ClickException(
            "当前为公共临时会话。需求/任务必须在已注册项目路径下执行，"
            "请先在目标目录运行 codepilot init，或使用 --project 指定已注册项目。"
        )
    runtime = _resolve_requirement_runtime(
        shell=shell,
        project_info=project_info,
        title=title,
        planner=planner,
        task_agent=task_agent,
        executor=executor,
        auto_commit=auto_commit,
        max_tasks=max_tasks,
        max_retries=max_retries,
    )
    decision = _run_requirement_decision_phase(
        shell=shell,
        project_info=project_info,
        runtime=runtime,
        priority=priority,
        execute=execute,
        task_source=task_source,
        work_item=work_item,
    )
    if json_mode:
        payload = _execute_requirement_json_phase(
            shell=shell,
            runtime=runtime,
            decision=decision,
        )
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    return _execute_requirement_plain_phase(
        shell=shell,
        runtime=runtime,
        decision=decision,
        quiet=quiet,
    )
