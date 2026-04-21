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

import inspect
import json
import sys
from pathlib import Path
from typing import Optional

import click

from codepilot import db
from codepilot.ai import (
    build_task_markdown_from_plan,
    normalize_agent_name,
)
from codepilot.config import find_config, load_config, load_project_config


def _shell():
    """Return the ``codepilot.commands.auto`` module for dynamic attribute lookup."""
    return sys.modules["codepilot.commands.auto"]


def _should_fallback_codex_planning(exc: Exception) -> bool:
    """Only fall back to a single Codex task on planner timeouts."""
    message = str(exc).lower()
    return "超时" in str(exc) or "timed out" in message or "timeout" in message


def _generate_task_breakdown_via_shell(shell, **kwargs):
    """Call the shell-exported planner with backward-compatible kwargs.

    ``run_requirement_workflow`` wants the raw planner payload so it can route
    everything through ``parse_automation_planner_result``. Older tests or
    external monkeypatches may still stub ``generate_task_breakdown`` with the
    historical signature that does not accept ``parse_result``; detect that and
    omit the flag instead of crashing with ``TypeError``.
    """
    planner_fn = shell.generate_task_breakdown
    signature = inspect.signature(planner_fn)
    supports_parse_flag = any(
        param.kind is inspect.Parameter.VAR_KEYWORD or name == "parse_result"
        for name, param in signature.parameters.items()
    )
    if supports_parse_flag:
        return planner_fn(parse_result=False, **kwargs)
    return planner_fn(**kwargs)


def clarify_requirement(
    title: str,
    *,
    project_info: dict,
    qa_history: Optional[list[dict]] = None,
    planner: str = "codex",
    max_turns: int = 3,
) -> dict:
    """Assess a requirement and ask for clarification if the intent is vague.

    Returns:
        ``{"status": "ready", "refined_title": str, "qa_history": [...]}`` when
        the planner can proceed, or
        ``{"status": "needs_clarification", "questions": [str, ...], "turn": int,
        "qa_history": [...]}`` when the caller should collect another round.

    The ``qa_history`` field is always returned so callers can persist it
    between turns (e.g. chat REPL, Web UI session state).
    """
    from codepilot.ai_clarify import assess_requirement
    from codepilot.config import load_project_config

    qa_history = list(qa_history or [])

    cfg = load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))
    if cfg and not getattr(cfg.automation, "clarify_vague_requirements", True):
        return {
            "status": "ready",
            "refined_title": title.strip(),
            "source": "disabled",
            "qa_history": qa_history,
        }

    classifier_cfg = getattr(cfg, "classifier", None) if cfg else None
    classifier_provider = classifier_cfg.provider if classifier_cfg and classifier_cfg.enabled else ""
    classifier_model = classifier_cfg.model if classifier_cfg and classifier_cfg.enabled else ""
    classifier_timeout = classifier_cfg.timeout if classifier_cfg and classifier_cfg.enabled else 30
    api_key = None
    base_url = None
    if cfg and classifier_provider:
        api_key = cfg.get_provider_api_key(classifier_provider)
        provider_cfg = cfg.providers.get(classifier_provider)
        base_url = provider_cfg.base_url if provider_cfg else None

    result = assess_requirement(
        title,
        project_path=project_info.get("path", ""),
        qa_history=qa_history,
        max_turns=max_turns,
        classifier_provider=classifier_provider,
        classifier_model=classifier_model,
        api_key=api_key,
        base_url=base_url,
        planner=planner,
        timeout=classifier_timeout or 30,
    )
    result["qa_history"] = qa_history
    return result


def resolve_project_for_prompt(project: Optional[str] = None, cwd: Optional[Path] = None) -> dict:
    """Resolve the target project, preferring the current working tree."""
    db.init_db()
    current_dir = Path(cwd or Path.cwd()).resolve()

    if project:
        proj = db.get_project(project)
        if not proj:
            raise click.ClickException(f"项目 '{project}' 未注册")
        return proj

    config_path = find_config(current_dir)
    if config_path:
        cfg = load_config(config_path)
        project_root = config_path.parent.resolve()
        matched = db.find_project_by_path(project_root)
        project_name = (cfg.project_name or cfg.project.name or project_root.name) if cfg else project_root.name
        if matched and Path(matched["path"]).resolve() == project_root:
            return db.register_project(
                name=matched["name"],
                path=str(project_root),
                base_branch=(cfg.base_branch if cfg else matched.get("base_branch", "dev")),
                default_mode=(cfg.default_mode if cfg else matched.get("default_mode", "dual")),
                worktree_base=(cfg.worktree_base if cfg else matched.get("worktree_base")),
                config_file=str(config_path),
            )

        return db.register_project(
            name=project_name,
            path=str(project_root),
            base_branch=(cfg.base_branch if cfg else "dev"),
            default_mode=(cfg.default_mode if cfg else "dual"),
            worktree_base=(cfg.worktree_base if cfg else None),
            config_file=str(config_path),
        )

    matched = db.find_project_by_path(current_dir)
    if matched:
        return matched

    if (current_dir / ".git").exists():
        return db.register_project(
            name=current_dir.name,
            path=str(current_dir),
            base_branch="main",
            default_mode="dual",
            config_file=None,
        )

    raise click.ClickException("未找到当前项目，请先运行 codepilot init，或在命令里显式指定 --project")


def _project_config(project_info: dict):
    return load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))


def _provider_context(project_info: dict) -> str:
    """Prefer an explicitly stored AGENTS.toml path when resolving CLI providers."""
    return project_info.get("config_file") or project_info["path"]


def _has_explicit_automation_task_agent(project_info: dict, cfg=None) -> bool:
    config_ref = project_info.get("config_file")
    if not config_ref:
        return bool(
            cfg
            and getattr(getattr(cfg, "automation", None), "task_agent", "")
            and getattr(cfg.automation, "task_agent", "") != "dual"
        )
    try:
        import tomllib

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
    normalized = normalize_agent_name(raw_agent)

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
    }:
        raise click.ClickException(
            f"内置执行器暂时不支持 `{raw_agent}`。请改用 codex、claude、claude-node 或 dual。"
        )

    return normalized


def run_requirement_workflow(
    *,
    project_info: dict,
    title: str,
    planner: str = "codex",
    task_agent: Optional[str] = None,
    priority: str = "P2",
    max_tasks: int = 5,
    execute: Optional[bool] = None,
    executor: str = "auto",
    auto_commit: bool = True,
    max_retries: int = 3,
    json_mode: bool = False,
    quiet: bool = False,
) -> dict:
    """Plan one natural-language requirement and optionally execute it."""
    from codepilot.output import echo

    shell = _shell()
    title = " ".join(title.strip().split())
    if not title:
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
    planner = effective["planner"]
    executor = effective["executor"]
    auto_commit = effective["auto_commit"]
    max_tasks = effective["max_tasks"]
    max_retries = effective["max_retries"]
    task_agent = shell._resolve_task_agent(project_info, task_agent, executor)

    cfg = load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))
    two_stage_enabled = True
    if cfg and not getattr(cfg.automation, "two_stage_planning", True):
        two_stage_enabled = False

    # Pull open tasks so the planner can dedup against the live backlog.
    existing_tasks = [
        t for t in db.list_tasks(project=project_name)
        if t.get("status") in {"backlog", "in_progress"}
    ]

    echo(f"[cyan]收到需求：{title}[/cyan]")
    if two_stage_enabled:
        echo(f"[dim]  正在用 {planner} 侦察项目 → 拆分任务，请稍候...[/dim]")
    else:
        echo(f"[dim]  正在用 {planner} 规划任务，请稍候...[/dim]")
    try:
        breakdown = _generate_task_breakdown_via_shell(
            shell,
            title=title,
            project_path=project_path,
            planner=planner,
            max_tasks=max_tasks,
            config_ref=_provider_context(project_info),
            two_stage=two_stage_enabled,
            existing_tasks=existing_tasks,
        )
    except Exception as exc:
        if normalize_agent_name(planner) == "codex" and _should_fallback_codex_planning(exc):
            echo("[yellow]Codex 规划没有及时完成，已降级为单任务直接执行。[/yellow]")
            breakdown = {
                "summary": "Codex 规划未完成，已降级为单任务执行",
                "complexity": "simple",
                "should_split": False,
                "tasks": [
                    {
                        "title": title,
                        "priority": priority,
                        "goal": title,
                        "acceptance_criteria": [
                            "完成当前需求的核心实现，主流程可以实际运行。",
                            "运行必要验证并在结果中说明是否通过。",
                        ],
                        "builder_notes": [
                            "先阅读相关代码，优先处理最影响可用性的阻塞点。",
                            "如果问题过大，先用最小可行改动把主链路跑通。",
                        ],
                        "reviewer_notes": [
                            "检查是否真的跑通主流程，而不只是修改文案或配置。",
                            "确认验证步骤和潜在回归风险已经说明。",
                        ],
                        "files": [],
                        "notes": [
                            f"本次为 Codex 规划失败后的降级执行。原始原因：{exc}",
                        ],
                    }
                ],
            }
        else:
            raise click.ClickException(str(exc)) from exc

    try:
        breakdown = shell.parse_automation_planner_result(
            breakdown,
            title=title,
            max_tasks=max_tasks,
            existing_tasks=existing_tasks,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    complexity = breakdown.get("complexity") or ("simple" if len(breakdown["tasks"]) <= 1 else "complex")
    should_split = breakdown.get("should_split")
    if should_split is None:
        should_split = len(breakdown["tasks"]) > 1

    # Surface dedup decisions up-front so the user knows why nothing / less
    # than expected got created.
    dedup_skipped = breakdown.get("dedup_skipped") or []
    for dup in dedup_skipped:
        echo(
            f"[yellow]跳过重复任务：[/yellow]「{dup.get('proposed_title') or ''}」"
            f" 已存在 #{dup.get('matched_existing_id')}"
            f"「{dup.get('matched_existing_title') or ''}」"
        )

    created_tasks = []
    previous_task_id: int | None = None
    created_ids_by_index: list[int] = []
    for idx, item in enumerate(breakdown["tasks"]):
        dep_indices = item.get("depends_on_indices") or []
        dep_ids = [
            created_ids_by_index[i]
            for i in dep_indices
            if isinstance(i, int) and 0 <= i < len(created_ids_by_index)
        ]
        if not dep_ids and previous_task_id and not item.get("depends_on_indices"):
            dep_ids = [previous_task_id]
        task = db.create_task(
            project=project_name,
            title=item["title"],
            content=build_task_markdown_from_plan(item),
            agent=task_agent,
            priority=item.get("priority") or priority,
            depends_on=dep_ids or None,
            project_path=project_path,
            max_retries=max_retries,
        )
        created_tasks.append(task)
        created_ids_by_index.append(task["id"])
        previous_task_id = task["id"]

    will_execute = _should_execute(project_info, execute)

    payload = {
        "project": project_name,
        "summary": breakdown.get("summary", ""),
        "complexity": complexity,
        "should_split": should_split,
        "task_agent": task_agent,
        "tasks": created_tasks,
        "will_execute": will_execute,
    }

    if json_mode:
        if will_execute:
            payload["run"] = shell.run_backlog(
                project_name,
                once=False,
                limit=len(created_tasks),
                executor=executor,
                auto_commit=auto_commit,
            )
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    label = "复杂任务" if should_split else "简单任务"
    echo(f"[green][OK] 已识别为{label}[/green]  complexity={complexity}")
    if breakdown.get("summary"):
        click.echo(f"  摘要: {breakdown['summary']}")
    for task in created_tasks:
        dep = f" depends_on=#{json.loads(task['depends_on'])[0]}" if task.get("depends_on") else ""
        click.echo(f"  #{task['id']}  {task['title']}  [{task['priority']}]  agent={task_agent}{dep}")
    if not quiet:
        shell.render_project_dashboard(project_name, include_done=False, max_rows=max(8, len(created_tasks)), title="任务面板")

    if not will_execute:
        echo()
        echo("[dim]已完成规划，未自动执行[/dim]")
        return payload

    echo()
    echo("[cyan]开始自动执行...[/cyan]")
    stats = shell.run_backlog(
        project_name,
        once=False,
        limit=len(created_tasks),
        executor=executor,
        auto_commit=auto_commit,
        retry_on_failure=False,
        quiet=quiet,
    )
    payload["run"] = stats
    echo(
        f"\n[dim]Workflow 完成: processed={stats['processed']} done={stats['done']} "
        f"failed={stats['failed']} requeued={stats['requeued']}[/dim]"
    )
    return payload
