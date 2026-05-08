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

from dataclasses import dataclass
import json
import sys
from pathlib import Path
from typing import Callable, Optional

import click

from codepilot.gateway.types import GatewayCallOptions
from codepilot.storage import database as db
from codepilot.ai_support.clarification_protocol import (
    build_clarification_answer_summary,
    normalize_clarification_answers as _normalize_protocol_answers,
    normalize_clarification_history as _normalize_protocol_history,
    normalize_clarification_questions as _normalize_protocol_questions,
    normalize_text as _normalize_protocol_text,
    render_clarification_questions,
)
from codepilot.ai_support.intent_rules import _heuristic_intent
from codepilot.commands import auto_project_resolution as _project_resolution
from codepilot.commands import auto_workflow_planning as _planning_flow
from codepilot.core.config import (
    load_project_config,
    resolve_project_config_reference,
)

TEMP_SESSION_NAME = _project_resolution.TEMP_SESSION_NAME


def _normalize_agent_name(value: str) -> str:
    from codepilot.ai_support.service import normalize_agent_name

    return normalize_agent_name(value)


def _build_task_markdown_from_plan(item: dict) -> str:
    from codepilot.ai_support.service import build_task_markdown_from_plan

    return build_task_markdown_from_plan(item)


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


def clarify_requirement(
    title: str,
    *,
    project_info: dict,
    qa_history: Optional[list[dict]] = None,
    planner: str = "codex",
    max_turns: int = 3,
    stream_callback: Callable[[str], None] | None = None,
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
    from codepilot.ai_support.clarify import assess_requirement

    qa_history = normalize_clarification_history(qa_history)
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
        stream_callback=stream_callback,
    )
    result["qa_history"] = qa_history
    return result


def normalize_requirement_text(text: str) -> str:
    """Normalize a free-text requirement into a planner-friendly single line."""
    return _normalize_protocol_text(text)


def append_clarification_answer(
    qa_history: Optional[list[dict]],
    *,
    answer: str = "",
    questions: Optional[list[dict]] = None,
    clarify_answers: Optional[list[dict]] = None,
) -> list[dict]:
    """Append one clarification Q/A turn with shared normalization rules."""
    merged_history = normalize_clarification_history(qa_history)
    normalized_questions = normalize_clarification_questions(questions)
    normalized_answers = normalize_clarification_answers(
        normalized_questions,
        raw_answers=clarify_answers,
        answer_text=answer,
    )
    summary = build_clarification_answer_summary(normalized_answers) or normalize_requirement_text(answer)
    if not normalized_answers and not summary:
        return merged_history
    merged_history.append({
        "questions": normalized_questions,
        "answers": normalized_answers,
        "answer": summary,
    })
    return merged_history


_DEFAULT_CLARIFICATION_INTENT = "requirement"


def normalize_clarification_questions(questions: Optional[list[dict]]) -> list[dict]:
    return _normalize_protocol_questions(questions)


def normalize_clarification_answers(
    questions: Optional[list[dict]],
    *,
    raw_answers: Optional[list[dict]] = None,
    answer_text: str = "",
) -> list[dict]:
    return _normalize_protocol_answers(
        questions,
        raw_answers=raw_answers,
        answer_text=answer_text,
    )


def normalize_clarification_history(qa_history: Optional[list[dict]]) -> list[dict]:
    return _normalize_protocol_history(qa_history)


def build_clarification_state(
    *,
    original_title: str,
    qa_history: Optional[list[dict]] = None,
    last_questions: Optional[list[dict]] = None,
    intent: str = _DEFAULT_CLARIFICATION_INTENT,
) -> dict:
    """Build normalized clarification session state shared by chat/webui/go."""
    normalized_intent = normalize_requirement_text(intent).lower() or _DEFAULT_CLARIFICATION_INTENT
    return {
        "original_title": normalize_requirement_text(original_title),
        "qa_history": normalize_clarification_history(qa_history),
        "last_questions": normalize_clarification_questions(last_questions),
        "intent": normalized_intent,
    }


def append_clarification_answer_to_state(
    state: Optional[dict],
    *,
    answer: str = "",
    questions: Optional[list[dict]] = None,
    clarify_answers: Optional[list[dict]] = None,
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
        else normalize_clarification_questions(questions)
    )
    merged_history = append_clarification_answer(
        base["qa_history"],
        answer=answer,
        questions=active_questions,
        clarify_answers=clarify_answers,
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


def continue_pending_clarification(
    pending_state: Optional[dict],
    *,
    answer: str = "",
    clarify_answers: Optional[list[dict]] = None,
    project_info: dict,
    planner: str = "codex",
    max_turns: int = 3,
    intent: str = _DEFAULT_CLARIFICATION_INTENT,
    clarify_fn=None,
    stream_callback: Callable[[str], None] | None = None,
) -> dict:
    """Advance one pending-clarification turn with normalized status/errors.

    Returns one of:
    - ``{"status": "needs_clarification", "pending_state": {...}, "questions": [...]}``
    - ``{"status": "ready", "refined_title": str}``
    - ``{"status": "error", "error_kind": "interrupt"|"click"|"runtime", "message": str}``
    """
    base_state = build_clarification_state(
        original_title=(pending_state or {}).get("original_title") or "",
        qa_history=(pending_state or {}).get("qa_history"),
        last_questions=(pending_state or {}).get("last_questions"),
        intent=(pending_state or {}).get("intent") or intent,
    )
    try:
        assessment = assess_requirement_for_planning(
            answer,
            project_info=project_info,
            planner=planner,
            qa_history=base_state["qa_history"],
            original_title=base_state["original_title"],
            last_questions=base_state["last_questions"],
            clarify_answers=clarify_answers,
            max_turns=max_turns,
            clarify_fn=clarify_fn,
            stream_callback=stream_callback,
        )
    except KeyboardInterrupt:
        return {
            "status": "error",
            "error_kind": "interrupt",
            "message": "clarification interrupted",
            "pending_state": base_state,
        }
    except click.ClickException as exc:
        return {
            "status": "error",
            "error_kind": "click",
            "message": exc.format_message(),
            "pending_state": base_state,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_kind": "runtime",
            "message": str(exc),
            "pending_state": base_state,
        }

    next_state = clarification_state_from_assessment(
        assessment=assessment,
        seed_title=base_state["original_title"],
        previous_state=base_state,
        intent=base_state["intent"],
    )
    if next_state:
        questions = next_state.get("last_questions") or []
        return {
            "status": "needs_clarification",
            "questions": questions,
            "pending_state": next_state,
            "assessment": assessment,
        }

    refined = normalize_requirement_text(
        assessment.get("refined_title") or base_state["original_title"]
    )
    return {
        "status": "ready",
        "refined_title": refined,
        "pending_state": None,
        "assessment": assessment,
    }


def assess_requirement_for_planning(
    text: str,
    *,
    project_info: dict,
    planner: str = "codex",
    qa_history: Optional[list[dict]] = None,
    original_title: str = "",
    last_questions: Optional[list[dict]] = None,
    clarify_answers: Optional[list[dict]] = None,
    max_turns: int = 3,
    clarify_fn=None,
    stream_callback: Callable[[str], None] | None = None,
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
            clarify_answers=clarify_answers,
        )

    clarifier = clarify_fn or _shell().clarify_requirement
    assessment = clarifier(
        seed_title,
        project_info=project_info,
        qa_history=merged_history,
        planner=planner,
        max_turns=max_turns,
        stream_callback=stream_callback,
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
    legacy_classifier: bool = False,
    default_intent: str = "requirement",
) -> str:
    """Classify user input intent using the shared auto/chat/webui chain."""
    forced = (category or "auto").strip().lower()
    valid = {"question", "task", "requirement", "command"}
    if forced in valid:
        return forced

    if not legacy_classifier:
        try:
            guess = _heuristic_intent(text)
        except Exception:
            guess = None
        fallback = (default_intent or "").strip().lower()
        return guess if guess in valid else (fallback if fallback in valid else "")

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
        "  任务日志:  codepilot task logs <task_id>",
        "  重试任务:  codepilot task retry <task_id>",
        "  停止任务:  codepilot task stop <task_id>",
        "  触发巡检:  codepilot inspect -p <项目>",
    ]
    if include_release:
        lines.append("  发布打包:  codepilot binary prepare --version <版本>")
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
) -> list[dict]:
    return _planning_flow.create_tasks_from_breakdown(
        breakdown=breakdown,
        project_name=project_name,
        project_path=project_path,
        task_agent=task_agent,
        priority=priority,
        max_retries=max_retries,
        task_source=task_source,
        build_task_markdown_from_plan=_build_task_markdown_from_plan,
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

