"""Planning/task-creation helpers extracted from ``auto_workflow``.

This module keeps the heavy planning and fallback branches isolated so the
public ``codepilot.commands.auto_workflow`` shell can stay focused on the
entrypoint/compatibility layer.
"""

from __future__ import annotations

import inspect
from typing import Callable

import click

from codepilot.core import progress_bus
from codepilot.core.work_item import coerce_work_item
from codepilot.commands.task_quality import evaluate_planning_breakdown as _evaluate_planning_breakdown
from codepilot.storage import database as db


def should_fallback_codex_planning(exc: Exception) -> bool:
    """Only fall back to a single Codex task on planner timeouts."""
    message = str(exc).lower()
    return "超时" in str(exc) or "timed out" in message or "timeout" in message


def is_claude_family_planner(
    planner: str,
    *,
    normalize_agent_name: Callable[[str], str],
) -> bool:
    return normalize_agent_name(planner).startswith("claude")


def generate_task_breakdown_via_shell(shell, **kwargs):
    """Call the shell-exported planner with backward-compatible kwargs."""
    planner_fn = shell.generate_task_breakdown
    signature = inspect.signature(planner_fn)
    supports_parse_flag = any(
        param.kind is inspect.Parameter.VAR_KEYWORD or name == "parse_result"
        for name, param in signature.parameters.items()
    )
    if supports_parse_flag:
        return planner_fn(parse_result=False, **kwargs)
    return planner_fn(**kwargs)


def _emit_planning_progress(message: str, *, stage: str = "planner", level: str = "info", extra: dict | None = None) -> None:
    try:
        progress_bus.emit(
            stage=stage,
            message=message,
            level=level,
            event_type="planning",
            extra={"phase_kind": stage, **dict(extra or {})},
        )
    except Exception:
        pass


def fallback_single_task_breakdown(*, title: str, priority: str, exc: Exception) -> dict:
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
                "depends_on_indices": [],
                "risk_level": "medium",
                "scope_budget": "unplanned / single-task fallback",
                "evidence": (
                    "fallback mode — no recon evidence collected; "
                    "task derived directly from the raw user requirement"
                ),
            }
        ],
    }


_VALID_RISK_LEVELS = {"low", "medium", "high"}


def normalize_task_spec(item: dict) -> dict:
    """Normalize one planner task entry with defensive defaults."""
    if not isinstance(item, dict):
        return item

    risk_raw = str(item.get("risk_level") or "").strip().lower()
    item["risk_level"] = risk_raw if risk_raw in _VALID_RISK_LEVELS else "medium"

    budget_raw = str(item.get("scope_budget") or "").strip()
    item["scope_budget"] = budget_raw or "unspecified"

    evidence_raw = str(item.get("evidence") or "").strip()
    item["evidence"] = evidence_raw

    deps = item.get("depends_on_indices")
    if not isinstance(deps, list):
        item["depends_on_indices"] = []
    else:
        item["depends_on_indices"] = [i for i in deps if isinstance(i, int) and i >= 0]

    return item


def list_existing_open_tasks(project_name: str) -> list[dict]:
    return [
        task for task in db.list_tasks(project=project_name)
        if task.get("status") in {"backlog", "in_progress"}
    ]


def evaluate_planning_quality(title: str, breakdown: dict) -> tuple[list[str], list[str]]:
    return _evaluate_planning_breakdown(title, breakdown)


def render_quality_feedback(blocking: list[str], advisory: list[str], *, limit: int = 8) -> str:
    issues = [*blocking, *advisory]
    lines = [f"{idx}. {item}" for idx, item in enumerate(issues[:limit], 1)]
    if len(issues) > limit:
        lines.append(f"{limit + 1}. Also address the remaining {len(issues) - limit} similar issues.")
    return "\n".join(lines)


def plan_requirement_breakdown(
    *,
    shell,
    title: str,
    planner: str,
    priority: str,
    max_tasks: int,
    project_name: str,
    project_path: str,
    config_ref: str,
    two_stage_enabled: bool,
    normalize_agent_name: Callable[[str], str],
    should_fallback_codex_planning_fn: Callable[[Exception], bool],
) -> dict:
    from codepilot.core.output import echo

    planning_text = "正在用 {planner} 侦察项目 → 拆分任务，请稍候..." if two_stage_enabled else "正在用 {planner} 规划任务，请稍候..."
    echo(f"[dim]  {planning_text.format(planner=planner)}[/dim]")
    _emit_planning_progress(
        planning_text.format(planner=planner),
        stage="planner",
        extra={"planner": planner, "two_stage": bool(two_stage_enabled)},
    )
    existing_tasks = list_existing_open_tasks(project_name)
    _emit_planning_progress(
        f"已读取当前项目未完成任务 {len(existing_tasks)} 个，用于避免重复规划。",
        stage="planner",
        extra={"open_task_count": len(existing_tasks)},
    )

    def _plan_once(active_planner: str, requirement_text: str) -> dict:
        _emit_planning_progress(
            f"开始调用 {active_planner} 生成任务拆分。",
            stage="planner",
            extra={"planner": active_planner},
        )
        raw_breakdown = generate_task_breakdown_via_shell(
            shell,
            title=requirement_text,
            project_path=project_path,
            planner=active_planner,
            max_tasks=max_tasks,
            config_ref=config_ref,
            two_stage=two_stage_enabled,
            existing_tasks=existing_tasks,
        )
        _emit_planning_progress(
            "规划器已返回结构化结果，正在解析和校验任务拆分。",
            stage="planner",
            extra={"planner": active_planner},
        )
        return shell.parse_automation_planner_result(
            raw_breakdown,
            title=title,
            max_tasks=max_tasks,
            existing_tasks=existing_tasks,
        )

    def _plan_with_quality_repair(active_planner: str) -> dict:
        first = _plan_once(active_planner, title)
        blocking, advisory = evaluate_planning_quality(title, first)
        if not blocking:
            if advisory:
                echo(f"[yellow]规划质量提醒：{len(advisory)} 个可优化项（继续执行当前规划）[/yellow]")
                _emit_planning_progress(
                    f"规划质量检查完成：有 {len(advisory)} 个可优化项，继续采用当前规划。",
                    stage="planner",
                    level="warning",
                    extra={"advisory_count": len(advisory)},
                )
            else:
                _emit_planning_progress("规划质量检查通过。", stage="planner")
            return first

        echo(f"[yellow]规划质量检查发现 {len(blocking)} 个阻塞问题，自动触发一次重规划...[/yellow]")
        _emit_planning_progress(
            f"规划质量检查发现 {len(blocking)} 个阻塞问题，自动触发一次重规划。",
            stage="planner",
            level="warning",
            extra={"blocking_count": len(blocking), "advisory_count": len(advisory)},
        )
        feedback = render_quality_feedback(blocking, advisory)
        corrected_title = (
            f"{title}\n\n"
            "Previous planning output needs correction:\n"
            f"{feedback}\n"
            "Regenerate tasks that are directly executable, aligned with the requirement, "
            "and strictly follow the task template fields."
        )
        second = _plan_once(active_planner, corrected_title)
        second_blocking, second_advisory = evaluate_planning_quality(title, second)
        if len(second_blocking) < len(blocking):
            if second_blocking:
                echo(
                    f"[yellow]重规划后仍有 {len(second_blocking)} 个阻塞问题，"
                    "但已优于首轮结果，继续采用重规划版本。[/yellow]"
                )
                _emit_planning_progress(
                    f"重规划后仍有 {len(second_blocking)} 个阻塞问题，但已优于首轮结果。",
                    stage="planner",
                    level="warning",
                    extra={"blocking_count": len(second_blocking)},
                )
            elif second_advisory:
                echo(
                    f"[yellow]重规划已消除阻塞问题，仍有 {len(second_advisory)} 个可优化项。[/yellow]"
                )
                _emit_planning_progress(
                    f"重规划已消除阻塞问题，仍有 {len(second_advisory)} 个可优化项。",
                    stage="planner",
                    level="warning",
                    extra={"advisory_count": len(second_advisory)},
                )
            else:
                _emit_planning_progress("重规划已消除阻塞问题。", stage="planner")
            return second

        echo(
            "[yellow]自动重规划未明显改善，本轮沿用首轮结果；"
            "建议后续补充更明确的需求边界。[/yellow]"
        )
        _emit_planning_progress(
            "自动重规划未明显改善，本轮沿用首轮结果。",
            stage="planner",
            level="warning",
        )
        return first

    def _codex_single_task_fallback(exc: Exception) -> dict:
        echo("[yellow]Codex 规划没有及时完成，已降级为单任务直接执行。[/yellow]")
        _emit_planning_progress(
            f"Codex 规划没有及时完成，降级为单任务执行：{exc}",
            stage="planner",
            level="warning",
        )
        breakdown = fallback_single_task_breakdown(title=title, priority=priority, exc=exc)
        return shell.parse_automation_planner_result(
            breakdown,
            title=title,
            max_tasks=max_tasks,
            existing_tasks=existing_tasks,
        )

    try:
        return _plan_with_quality_repair(planner)
    except Exception as primary_exc:
        normalized = normalize_agent_name(planner)
        if normalized == "codex" and should_fallback_codex_planning_fn(primary_exc):
            return _codex_single_task_fallback(primary_exc)

        if is_claude_family_planner(planner, normalize_agent_name=normalize_agent_name):
            echo("[yellow]Claude 规划失败，正在重试一次...[/yellow]")
            _emit_planning_progress(
                "Claude 规划失败，正在重试一次。",
                stage="planner",
                level="warning",
            )
            try:
                return _plan_with_quality_repair(planner)
            except Exception as retry_exc:
                echo("[yellow]Claude 规划仍失败，已回退到 codex 继续规划。[/yellow]")
                echo(f"[dim]  Claude 原始原因：{retry_exc}[/dim]")
                _emit_planning_progress(
                    f"Claude 规划仍失败，回退到 Codex：{retry_exc}",
                    stage="planner",
                    level="warning",
                )
                try:
                    return _plan_with_quality_repair("codex")
                except Exception as codex_exc:
                    if should_fallback_codex_planning_fn(codex_exc):
                        return _codex_single_task_fallback(codex_exc)
                    raise click.ClickException(
                        f"Claude 规划失败，且回退 Codex 也失败：{codex_exc}"
                    ) from codex_exc

        raise click.ClickException(str(primary_exc)) from primary_exc


def echo_dedup_skips(dedup_skipped: list[dict]) -> None:
    from codepilot.core.output import echo

    for dup in dedup_skipped:
        echo(
            f"[yellow]跳过重复任务：[/yellow]「{dup.get('proposed_title') or ''}」"
            f" 已存在 #{dup.get('matched_existing_id')}"
            f"「{dup.get('matched_existing_title') or ''}」"
        )


def derive_breakdown_meta(breakdown: dict) -> tuple[str, bool]:
    complexity = breakdown.get("complexity") or ("simple" if len(breakdown["tasks"]) <= 1 else "complex")
    should_split = breakdown.get("should_split")
    if should_split is None:
        should_split = len(breakdown["tasks"]) > 1
    return complexity, bool(should_split)


def create_tasks_from_breakdown(
    *,
    breakdown: dict,
    project_name: str,
    project_path: str,
    task_agent: str,
    priority: str,
    max_retries: int,
    build_task_markdown_from_plan: Callable[[dict], str],
    task_source: str = "user",
    work_item: dict | None = None,
) -> list[dict]:
    created_tasks: list[dict] = []
    previous_task_id: int | None = None
    created_ids_by_index: list[int] = []
    normalized_work_item = coerce_work_item(
        work_item,
        fallback_source=task_source or "user",
    ) if work_item is not None else None
    for item in breakdown["tasks"]:
        task_spec = normalize_task_spec(dict(item))
        dep_indices = task_spec.get("depends_on_indices") or []
        dep_ids = [
            created_ids_by_index[i]
            for i in dep_indices
            if isinstance(i, int) and 0 <= i < len(created_ids_by_index)
        ]
        if not dep_ids and previous_task_id and not dep_indices:
            dep_ids = [previous_task_id]
        task_spec["agent"] = task_agent
        task_work_item = None
        task_source_value = task_source or "user"
        if normalized_work_item is not None:
            task_work_item = coerce_work_item(
                normalized_work_item,
                fallback_source=task_source_value,
                fallback_raw_text=item.get("title") or "",
            )
            task_source_value = task_work_item["source"] or task_source_value
        task = db.create_task(
            project=project_name,
            title=item["title"],
            content=build_task_markdown_from_plan(task_spec),
            agent=task_agent,
            priority=item.get("priority") or priority,
            depends_on=dep_ids or None,
            project_path=project_path,
            max_retries=max_retries,
            source=task_source_value,
            work_item=task_work_item,
        )
        _emit_planning_progress(
            f"已创建任务 #{task['id']}：{task['title']}",
            stage="planner",
            extra={"task_id": task["id"]},
        )
        created_tasks.append(task)
        created_ids_by_index.append(task["id"])
        previous_task_id = task["id"]
    return created_tasks
