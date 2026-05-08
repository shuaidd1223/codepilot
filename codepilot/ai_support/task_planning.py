"""Helpers for planner-stage task breakdown flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional


def _dispatch_schema_prompt(
    planner_normalized: str,
    *,
    runners: dict[str, Callable[..., dict]],
    prompt: str,
    schema: dict,
    project_path: str,
    config_ref: Any,
) -> dict:
    """Pick the right schema-prompt runner via the CLI family registry.

    ``runners`` is a ``{family_name: callable}`` map keyed by canonical
    family name (claude / codex / opencode / …). Claude variants
    (claude-sonnet / opus / haiku / claude-node) all dispatch to the
    ``claude`` runner — the runner itself picks the model alias.
    """
    from codepilot.ai_support.cli_families import get_family

    family = get_family(planner_normalized)
    if family is None:
        # Try prefix-matching (claude-sonnet -> claude) before giving up.
        lowered = (planner_normalized or "").strip().lower()
        for name in runners:
            if lowered.startswith(name + "-") or lowered == name:
                family_name = name
                break
        else:
            raise RuntimeError(
                f"当前自动拆分暂时不支持规划器 `{planner_normalized}`。"
                "请改用 claude / codex / opencode 之一。"
            )
    else:
        family_name = family.name

    runner = runners.get(family_name)
    if runner is None:
        raise RuntimeError(
            f"未注入 `{family_name}` 的 schema_prompt runner。这是 service 层的接线遗漏。"
        )

    if family_name == "claude":
        return runner(
            prompt,
            schema,
            planner=planner_normalized,
            project_path=project_path,
            config_ref=config_ref,
        )
    return runner(
        prompt,
        schema,
        project_path=project_path,
        config_ref=config_ref,
    )


def build_task_markdown_from_plan(
    task: dict,
    *,
    normalize_agent_name: Callable[[str], str],
    template_path: Path,
) -> str:
    """Convert a structured plan item into task markdown using task templates."""

    class _SafeFormat(dict):
        def __missing__(self, key: str) -> str:  # type: ignore[override]
            return ""

    def _bullet(items: list[str], fallback: str) -> str:
        rows = [str(item).strip() for item in (items or []) if str(item).strip()]
        return "\n".join(f"- {row}" for row in rows) if rows else f"- {fallback}"

    def _coerce_list(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        return []

    def _ac_matrix(criteria_items: list[str]) -> str:
        rows = [str(item).strip() for item in (criteria_items or []) if str(item).strip()]
        if not rows:
            rows = ["待补充"]
        header = (
            "| AC # | Criterion | Verification Command / Action | Expected Result | Evidence Location |\n"
            "| :--- | :--- | :--- | :--- | :--- |"
        )
        body_lines = []
        for i, row in enumerate(rows):
            safe = row.replace("|", "\\|")
            body_lines.append(f"| AC-{i + 1} | {safe} |  |  |  |")
        return header + "\n" + "\n".join(body_lines)

    title = str(task.get("title") or "").strip() or "未命名任务"
    goal = str(task.get("goal") or "").strip() or "待补充"
    acceptance_items = _coerce_list(task.get("acceptance_criteria"))
    acceptance = _bullet(acceptance_items, "待补充")
    ac_matrix = _ac_matrix(acceptance_items)
    builder_notes = _bullet(_coerce_list(task.get("builder_notes")), "待补充")
    reviewer_notes = _bullet(_coerce_list(task.get("reviewer_notes")), "待补充")
    files = _bullet(_coerce_list(task.get("files")), "待确认")
    notes = _bullet(_coerce_list(task.get("notes")), "无")
    forbidden = _bullet(_coerce_list(task.get("forbidden")), "不改动任务声明范围外的生产代码；不做无关重构。")
    not_in_scope = _bullet(_coerce_list(task.get("not_in_scope")), "与本任务目标无关的模块、文档、部署流程。")
    priority = str(task.get("priority") or "").strip() or "P2"
    risk_level = str(task.get("risk_level") or "").strip() or "待评估"
    scope_budget = str(task.get("scope_budget") or "").strip() or "未设定"
    owner = str(task.get("owner") or "").strip() or "未指派"
    evidence = str(task.get("evidence") or "").strip() or "（未提供规划依据，建议人工复核）"
    dep_indices_raw = task.get("depends_on_indices")
    dep_indices = (
        [i for i in dep_indices_raw if isinstance(i, int) and i >= 0]
        if isinstance(dep_indices_raw, list)
        else []
    )
    depends_on = "无" if not dep_indices else ", ".join(f"T{idx + 1}" for idx in dep_indices)

    normalized_agent = normalize_agent_name(str(task.get("agent") or "dual"))
    template_text = template_path.read_text(encoding="utf-8", errors="replace")
    return template_text.format_map(
        _SafeFormat(
            {
                "title": title,
                "agent": normalized_agent,
                "priority": priority,
                "depends_on": depends_on,
                "risk_level": risk_level,
                "scope_budget": scope_budget,
                "owner": owner,
                "evidence": evidence,
                "goal": goal,
                "criteria": acceptance,
                "ac_matrix": ac_matrix,
                "requirements": builder_notes,
                "builder_responsibilities": builder_notes,
                "reviewer_responsibilities": reviewer_notes,
                "files": files,
                "forbidden": forbidden,
                "not_in_scope": not_in_scope,
                "notes": notes,
            }
        )
    )


def run_recon_stage(
    title: str,
    project_path: str,
    *,
    planner_normalized: str,
    config_ref: str | Path | None,
    project_context: str,
    progress_prefix: str = "  [recon]",
    run_claude_schema_prompt: Callable[..., dict],
    run_codex_schema_prompt: Callable[..., dict],
    run_opencode_schema_prompt: Callable[..., dict] | None = None,
    validate_recon_payload: Callable[[dict, str, str], tuple[dict, list[str]]],
    get_progress_callback: Callable[[], Callable[[str], None] | None],
) -> dict:
    """Run reconnaissance before the planning stage."""
    from codepilot.ai_support.prompts import RECON_PROMPT_TEMPLATE, RECON_SCHEMA

    prompt = RECON_PROMPT_TEMPLATE.format(
        title=title,
        project_context=project_context or "(No project context; inspect via tools.)",
    )

    callback = get_progress_callback()
    if callback:
        try:
            callback(f"{progress_prefix} 启动侦察：读取相关文件，梳理现状...")
        except Exception:
            pass

    runners: dict[str, Callable[..., dict]] = {
        "claude": run_claude_schema_prompt,
        "codex": run_codex_schema_prompt,
    }
    if run_opencode_schema_prompt is not None:
        runners["opencode"] = run_opencode_schema_prompt

    try:
        payload = _dispatch_schema_prompt(
            planner_normalized,
            runners=runners,
            prompt=prompt,
            schema=RECON_SCHEMA,
            project_path=project_path,
            config_ref=config_ref,
        )
    except RuntimeError as runtime_exc:
        # Recon is best-effort: unknown planner / missing runner shouldn't block planning.
        if callback:
            try:
                callback(f"{progress_prefix} 跳过侦察：{runtime_exc}")
            except Exception:
                pass
        return {}
    except Exception as exc:
        if callback:
            try:
                callback(f"{progress_prefix} 侦察失败，跳过直接进规划：{exc}")
            except Exception:
                pass
        return {}

    if not isinstance(payload, dict):
        return {}

    cleaned, dropped = validate_recon_payload(payload, project_path, title=title)
    callback = get_progress_callback()
    if callback:
        try:
            kept = cleaned.get("relevant_files") or []
            if dropped:
                callback(
                    f"{progress_prefix} 侦察完成：认定 {len(kept)} 个相关文件；"
                    f"丢弃 {len(dropped)} 个不存在的路径（{', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''}）"
                )
            else:
                callback(f"{progress_prefix} 侦察完成：认定 {len(kept)} 个相关文件")
        except Exception:
            pass
    return cleaned


def format_recon_block(recon: dict) -> str:
    """Render recon payload as a readable block for the planner prompt."""
    if not recon:
        return "(No recon conclusions were produced; plan based on project context.)"
    lines: list[str] = []
    current = (recon.get("current_state") or "").strip()
    if current:
        lines.append(f"Current state: {current}")
    files = [f for f in (recon.get("relevant_files") or []) if isinstance(f, str) and f.strip()]
    if files:
        lines.append("Relevant files:")
        for filename in files[:12]:
            lines.append(f"  - {filename}")
    findings = [item for item in (recon.get("key_findings") or []) if isinstance(item, str) and item.strip()]
    if findings:
        lines.append("Key findings:")
        for item in findings[:8]:
            lines.append(f"  - {item}")
    risks = [item for item in (recon.get("risks") or []) if isinstance(item, str) and item.strip()]
    if risks:
        lines.append("Risks:")
        for item in risks[:6]:
            lines.append(f"  - {item}")
    approach = (recon.get("suggested_approach") or "").strip()
    if approach:
        lines.append(f"Suggested approach: {approach}")
    return "\n".join(lines) if lines else "(Recon output is empty.)"


def parse_automation_planner_result(
    breakdown: dict | str,
    *,
    title: str,
    max_tasks: int = 5,
    existing_tasks: Optional[list[dict]] = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict:
    """Normalize planner output via the dedicated parser module."""
    from codepilot.ai_support.planner_parse import parse_automation_planner_result as _parse_result

    return _parse_result(
        breakdown,
        title=title,
        max_tasks=max_tasks,
        existing_tasks=existing_tasks,
        progress_callback=progress_callback,
    )


def generate_task_breakdown(
    title: str,
    project_path: str = "",
    planner: str = "codex",
    max_tasks: int = 5,
    config_ref: str | Path | None = None,
    *,
    two_stage: bool = True,
    parse_result: bool = True,
    existing_tasks: Optional[list[dict]] = None,
    normalize_agent_name: Callable[[str], str],
    collect_planner_context: Callable[[str, str], str],
    format_existing_block: Callable[[Optional[list[dict]]], str],
    run_recon_stage_fn: Callable[..., dict],
    format_recon_block_fn: Callable[[dict], str],
    run_claude_schema_prompt: Callable[..., dict],
    run_codex_schema_prompt: Callable[..., dict],
    run_opencode_schema_prompt: Callable[..., dict] | None = None,
    parse_automation_planner_result_fn: Callable[..., dict],
) -> dict:
    """Generate a structured subtask breakdown for a high-level goal."""
    from codepilot.ai_support.prompts import TASK_BREAKDOWN_PROMPT_TEMPLATE, TASK_BREAKDOWN_SCHEMA

    max_tasks = max(1, min(max_tasks, 8))
    normalized = normalize_agent_name(planner)
    context = collect_planner_context(project_path, title)

    recon: dict = {}
    if two_stage:
        recon = run_recon_stage_fn(
            title,
            project_path,
            planner_normalized=normalized,
            config_ref=config_ref,
            project_context=context,
        )

    recon_block = format_recon_block_fn(recon)
    existing_block = format_existing_block(existing_tasks)
    prompt = TASK_BREAKDOWN_PROMPT_TEMPLATE.format(
        title=title,
        project_context=context or "(Context collection failed; plan from requirement only.)",
        recon_block=recon_block,
        existing_tasks_block=existing_block,
        max_tasks=max_tasks,
    )

    runners: dict[str, Callable[..., dict]] = {
        "claude": run_claude_schema_prompt,
        "codex": run_codex_schema_prompt,
    }
    if run_opencode_schema_prompt is not None:
        runners["opencode"] = run_opencode_schema_prompt

    breakdown = _dispatch_schema_prompt(
        normalized,
        runners=runners,
        prompt=prompt,
        schema=TASK_BREAKDOWN_SCHEMA,
        project_path=project_path,
        config_ref=config_ref,
    )

    if not parse_result:
        return breakdown

    return parse_automation_planner_result_fn(
        breakdown,
        title=title,
        max_tasks=max_tasks,
        existing_tasks=existing_tasks,
    )
