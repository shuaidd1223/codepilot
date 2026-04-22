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
import tempfile
from pathlib import Path
from typing import Optional

import click

from codepilot.ai_gateway_types import GatewayCallOptions
from codepilot import db
from codepilot.commands import auto_project_resolution as _project_resolution
from codepilot.config import (
    load_project_config,
    resolve_project_config_reference,
)

TEMP_SESSION_NAME = _project_resolution.TEMP_SESSION_NAME


def _normalize_agent_name(value: str) -> str:
    from codepilot.ai import normalize_agent_name

    return normalize_agent_name(value)


def _build_task_markdown_from_plan(item: dict) -> str:
    from codepilot.ai import build_task_markdown_from_plan

    return build_task_markdown_from_plan(item)


def _is_subpath(path: Path, base: Path) -> bool:
    return _project_resolution._is_subpath(path, base)


def _is_temporary_workspace(path: Path) -> bool:
    """Cross-platform temporary workspace probe.

    Rules:
    - Any path under current user's home directory is treated as temporary
      when it is not an explicitly registered project.
    - System temp directory is also treated as temporary.
    """
    home = Path.home().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    return _is_subpath(path, home) or _is_subpath(path, temp_root)


def _build_temporary_session(path: Path) -> dict:
    return _project_resolution._build_temporary_session(path)


def _register_guidance(path: Path) -> str:
    return _project_resolution._register_guidance(path)


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

    qa_history = list(qa_history or [])
    runtime = _classifier_runtime(project_info)
    cfg = runtime["config"]
    if cfg and not getattr(cfg.automation, "clarify_vague_requirements", True):
        return {
            "status": "ready",
            "refined_title": title.strip(),
            "source": "disabled",
            "qa_history": qa_history,
        }

    result = assess_requirement(
        title,
        project_path=runtime["project_path"],
        config_ref=runtime["config_ref"],
        qa_history=qa_history,
        max_turns=max_turns,
        classifier_provider=runtime["classifier_provider"],
        classifier_model=runtime["classifier_model"],
        api_key=runtime["api_key"],
        base_url=runtime["base_url"],
        planner=planner,
        timeout=runtime["classifier_timeout"] or 30,
    )
    result["qa_history"] = qa_history
    return result


def normalize_requirement_text(text: str) -> str:
    """Normalize a free-text requirement into a planner-friendly single line."""
    return " ".join((text or "").split())


def append_clarification_answer(
    qa_history: Optional[list[dict]],
    *,
    answer: str,
    questions: Optional[list[str]] = None,
) -> list[dict]:
    """Append one clarification Q/A turn with shared normalization rules."""
    merged_history = list(qa_history or [])
    normalized_answer = normalize_requirement_text(answer)
    if not normalized_answer:
        return merged_history
    normalized_questions = [
        q.strip()
        for q in (questions or [])
        if isinstance(q, str) and q.strip()
    ]
    merged_history.append({
        "question": " | ".join(normalized_questions),
        "answer": normalized_answer,
    })
    return merged_history


_DEFAULT_CLARIFICATION_INTENT = "requirement"


def _normalize_clarification_questions(questions: Optional[list[str]]) -> list[str]:
    return [
        normalize_requirement_text(q)
        for q in (questions or [])
        if isinstance(q, str) and normalize_requirement_text(q)
    ]


def _normalize_clarification_history(qa_history: Optional[list[dict]]) -> list[dict]:
    rows: list[dict] = []
    for item in (qa_history or []):
        if not isinstance(item, dict):
            continue
        question = normalize_requirement_text(str(item.get("question") or ""))
        answer = normalize_requirement_text(str(item.get("answer") or ""))
        if not question and not answer:
            continue
        rows.append({
            "question": question,
            "answer": answer,
        })
    return rows


def build_clarification_state(
    *,
    original_title: str,
    qa_history: Optional[list[dict]] = None,
    last_questions: Optional[list[str]] = None,
    intent: str = _DEFAULT_CLARIFICATION_INTENT,
) -> dict:
    """Build normalized clarification session state shared by chat/webui/go."""
    normalized_intent = normalize_requirement_text(intent).lower() or _DEFAULT_CLARIFICATION_INTENT
    return {
        "original_title": normalize_requirement_text(original_title),
        "qa_history": _normalize_clarification_history(qa_history),
        "last_questions": _normalize_clarification_questions(last_questions),
        "intent": normalized_intent,
    }


def append_clarification_answer_to_state(
    state: Optional[dict],
    *,
    answer: str,
    questions: Optional[list[str]] = None,
) -> dict:
    """Append one user answer to clarification state and return a new state."""
    base = build_clarification_state(
        original_title=(state or {}).get("original_title") or "",
        qa_history=(state or {}).get("qa_history"),
        last_questions=(state or {}).get("last_questions"),
        intent=(state or {}).get("intent") or _DEFAULT_CLARIFICATION_INTENT,
    )
    active_questions = (
        base["last_questions"]
        if questions is None
        else _normalize_clarification_questions(questions)
    )
    merged_history = append_clarification_answer(
        base["qa_history"],
        answer=answer,
        questions=active_questions,
    )
    return build_clarification_state(
        original_title=base["original_title"],
        qa_history=merged_history,
        last_questions=active_questions,
        intent=base["intent"],
    )


def clarification_state_from_assessment(
    *,
    assessment: dict,
    seed_title: str,
    previous_state: Optional[dict] = None,
    intent: str = _DEFAULT_CLARIFICATION_INTENT,
) -> Optional[dict]:
    """Return normalized clarification state when assessment asks another round."""
    if (assessment or {}).get("status") != "needs_clarification":
        return None
    prev = previous_state or {}
    questions = (assessment or {}).get("questions")
    return build_clarification_state(
        original_title=prev.get("original_title") or seed_title,
        qa_history=(assessment or {}).get("qa_history") or prev.get("qa_history"),
        last_questions=questions if isinstance(questions, list) else prev.get("last_questions"),
        intent=prev.get("intent") or intent,
    )


def assess_requirement_for_planning(
    text: str,
    *,
    project_info: dict,
    planner: str = "codex",
    qa_history: Optional[list[dict]] = None,
    original_title: str = "",
    last_questions: Optional[list[str]] = None,
    max_turns: int = 3,
    clarify_fn=None,
) -> dict:
    """Shared entrypoint for clarification + planning input construction.

    This keeps chat/webui/go aligned on how ``seed_title`` / ``qa_history`` are
    assembled before calling :func:`clarify_requirement`.
    """
    normalized_text = normalize_requirement_text(text)
    normalized_original = normalize_requirement_text(original_title)
    seed_title = normalized_original or normalized_text

    merged_history = list(qa_history or [])
    if normalized_original:
        merged_history = append_clarification_answer(
            merged_history,
            answer=normalized_text,
            questions=last_questions,
        )

    clarifier = clarify_fn or _shell().clarify_requirement
    assessment = clarifier(
        seed_title,
        project_info=project_info,
        qa_history=merged_history,
        planner=planner,
        max_turns=max_turns,
    )
    result = dict(assessment)
    result["seed_title"] = seed_title
    result["qa_history"] = assessment.get("qa_history") or merged_history

    if result.get("status") == "ready":
        result["refined_title"] = normalize_requirement_text(
            result.get("refined_title") or seed_title
        )
    return result


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
        is_temporary_workspace_fn=_is_temporary_workspace,
    )


def _project_config(project_info: dict):
    return load_project_config(project_info)


def _provider_context(project_info: dict) -> str:
    """Prefer an explicitly stored AGENTS.toml path when resolving CLI providers."""
    return str(resolve_project_config_reference(project_info) or project_info["path"])


def _classifier_runtime(project_info: dict) -> dict:
    """Resolve shared classifier runtime options for chat/go/webui entries."""
    cfg = _project_config(project_info)
    classifier_cfg = getattr(cfg, "classifier", None) if cfg else None
    enabled = bool(classifier_cfg and classifier_cfg.enabled)
    provider = (classifier_cfg.provider or "") if enabled else ""
    model = (classifier_cfg.model or "") if enabled else ""
    timeout = int(classifier_cfg.timeout or 30) if enabled else 30
    api_key = None
    base_url = None
    if cfg and provider:
        api_key = cfg.get_provider_api_key(provider)
        provider_cfg = cfg.providers.get(provider)
        base_url = provider_cfg.base_url if provider_cfg else None
    return {
        "config": cfg,
        "project_path": project_info.get("path", ""),
        "config_ref": _provider_context(project_info),
        "classifier_provider": provider,
        "classifier_model": model,
        "classifier_timeout": timeout,
        "api_key": api_key,
        "base_url": base_url,
    }


def resolve_shared_gateway_options(project_info: dict) -> GatewayCallOptions:
    """Build one shared gateway context for classifier + question-answer paths."""
    runtime = _classifier_runtime(project_info)
    return GatewayCallOptions(
        classifier_provider=runtime["classifier_provider"],
        classifier_model=runtime["classifier_model"],
        api_key=runtime["api_key"],
        base_url=runtime["base_url"],
        project_path=runtime["project_path"],
        config_ref=runtime["config_ref"],
        planner="claude",
        timeout=runtime["classifier_timeout"] or 30,
    )


def resolve_question_answer_options(project_info: dict) -> dict:
    """Return unified gateway options for question-answer paths."""
    gateway_options = resolve_shared_gateway_options(project_info)
    return {
        "provider_key": gateway_options.classifier_provider,
        "model_override": gateway_options.classifier_model,
        "project_path": gateway_options.project_path,
        "config_ref": gateway_options.config_ref,
        "api_key": gateway_options.api_key,
        "base_url": gateway_options.base_url,
    }


def classify_entry_intent(
    text: str,
    *,
    project_info: dict,
    category: str = "auto",
    gateway_options: Optional[GatewayCallOptions] = None,
) -> str:
    """Classify user input intent using the shared auto/chat/webui chain."""
    forced = (category or "auto").strip().lower()
    valid = {"question", "task", "requirement", "command"}
    if forced in valid:
        return forced

    shared_options = gateway_options or resolve_shared_gateway_options(project_info)
    shell = _shell()
    try:
        result = shell.classify_intent(
            text,
            gateway_options=shared_options,
        )
        intent = (result.get("intent") or "").strip().lower()
        if intent in valid:
            return intent
    except Exception:
        pass
    return "requirement"


def command_intent_guidance(*, include_release: bool = False) -> str:
    """Return consistent guidance when input is classified as CLI command intent."""
    lines = [
        "这看起来是在调用 codepilot 自身命令，请在终端直接执行：",
        "  状态总览:  codepilot status -p <项目> -v",
        "  任务日志:  codepilot logs <task_id>",
        "  重试任务:  codepilot retry <task_id>",
        "  停止任务:  codepilot stop <task_id>",
        "  触发巡检:  codepilot inspect -p <项目>",
    ]
    if include_release:
        lines.append("  发布打包:  codepilot release prepare --version <版本>")
    return "\n".join(lines)


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
    }:
        raise click.ClickException(
            f"内置执行器暂时不支持 `{raw_agent}`。请改用 codex、claude、claude-node 或 dual。"
        )

    return normalized


def _fallback_single_task_breakdown(*, title: str, priority: str, exc: Exception) -> dict:
    return {
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


def _resolve_planning_mode(project_info: dict) -> bool:
    cfg = _project_config(project_info)
    return bool(not cfg or getattr(cfg.automation, "two_stage_planning", True))


def _list_existing_open_tasks(project_name: str) -> list[dict]:
    return [
        task for task in db.list_tasks(project=project_name)
        if task.get("status") in {"backlog", "in_progress"}
    ]


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
    from codepilot.output import echo

    planning_text = "正在用 {planner} 侦察项目 → 拆分任务，请稍候..." if two_stage_enabled else "正在用 {planner} 规划任务，请稍候..."
    echo(f"[dim]  {planning_text.format(planner=planner)}[/dim]")
    existing_tasks = _list_existing_open_tasks(project_name)

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
        if _normalize_agent_name(planner) == "codex" and _should_fallback_codex_planning(exc):
            echo("[yellow]Codex 规划没有及时完成，已降级为单任务直接执行。[/yellow]")
            breakdown = _fallback_single_task_breakdown(title=title, priority=priority, exc=exc)
        else:
            raise click.ClickException(str(exc)) from exc

    try:
        parsed = shell.parse_automation_planner_result(
            breakdown,
            title=title,
            max_tasks=max_tasks,
            existing_tasks=existing_tasks,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    return parsed


def _echo_dedup_skips(dedup_skipped: list[dict]) -> None:
    from codepilot.output import echo

    for dup in dedup_skipped:
        echo(
            f"[yellow]跳过重复任务：[/yellow]「{dup.get('proposed_title') or ''}」"
            f" 已存在 #{dup.get('matched_existing_id')}"
            f"「{dup.get('matched_existing_title') or ''}」"
        )


def _derive_breakdown_meta(breakdown: dict) -> tuple[str, bool]:
    complexity = breakdown.get("complexity") or ("simple" if len(breakdown["tasks"]) <= 1 else "complex")
    should_split = breakdown.get("should_split")
    if should_split is None:
        should_split = len(breakdown["tasks"]) > 1
    return complexity, bool(should_split)


def _create_tasks_from_breakdown(
    *,
    breakdown: dict,
    project_name: str,
    project_path: str,
    task_agent: str,
    priority: str,
    max_retries: int,
) -> list[dict]:
    created_tasks: list[dict] = []
    previous_task_id: int | None = None
    created_ids_by_index: list[int] = []
    for item in breakdown["tasks"]:
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
            content=_build_task_markdown_from_plan(item),
            agent=task_agent,
            priority=item.get("priority") or priority,
            depends_on=dep_ids or None,
            project_path=project_path,
            max_retries=max_retries,
        )
        created_tasks.append(task)
        created_ids_by_index.append(task["id"])
        previous_task_id = task["id"]
    return created_tasks


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
    from codepilot.output import echo

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
) -> dict:
    """Plan one natural-language requirement and optionally execute it."""
    from codepilot.output import echo

    shell = _shell()
    if project_info.get("is_temporary"):
        raise click.ClickException(
            "当前为公共临时会话。需求/任务必须在已注册项目路径下执行，"
            "请先在目标目录运行 codepilot init，或使用 --project 指定已注册项目。"
        )

    title = normalize_requirement_text(title)
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
    two_stage_enabled = _resolve_planning_mode(project_info)

    echo(f"[cyan]收到需求：{title}[/cyan]")
    breakdown = _plan_requirement_breakdown(
        shell=shell,
        title=title,
        planner=planner,
        priority=priority,
        max_tasks=max_tasks,
        project_name=project_name,
        project_path=project_path,
        project_info=project_info,
        two_stage_enabled=two_stage_enabled,
    )
    complexity, should_split = _derive_breakdown_meta(breakdown)
    _echo_dedup_skips(breakdown.get("dedup_skipped") or [])
    created_tasks = _create_tasks_from_breakdown(
        breakdown=breakdown,
        project_name=project_name,
        project_path=project_path,
        task_agent=task_agent,
        priority=priority,
        max_retries=max_retries,
    )

    will_execute = _should_execute(project_info, execute)
    payload = _build_requirement_payload(
        project_name=project_name,
        breakdown=breakdown,
        complexity=complexity,
        should_split=should_split,
        task_agent=task_agent,
        created_tasks=created_tasks,
        will_execute=will_execute,
    )

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

    _emit_non_json_plan_output(
        shell=shell,
        project_name=project_name,
        breakdown=breakdown,
        created_tasks=created_tasks,
        complexity=complexity,
        should_split=should_split,
        task_agent=task_agent,
        quiet=quiet,
    )

    if not will_execute:
        echo()
        echo("[dim]已完成规划，未自动执行[/dim]")
        return payload

    echo()
    echo("[cyan]开始自动执行...[/cyan]")
    stats = _run_requirement_backlog(
        shell=shell,
        project_name=project_name,
        task_count=len(created_tasks),
        executor=executor,
        auto_commit=auto_commit,
        quiet=quiet,
    )
    payload["run"] = stats
    echo(
        f"\n[dim]Workflow 完成: processed={stats['processed']} done={stats['done']} "
        f"failed={stats['failed']} requeued={stats['requeued']}[/dim]"
    )
    return payload
