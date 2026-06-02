"""Prompt construction helpers for the built-in executor."""

from __future__ import annotations

from pathlib import Path

from codepilot.prompts import load_prompt as _load_prompt
from codepilot.commands.reviewer_output import ReviewerVerdict, parse_reviewer_output
from codepilot.commands.run_builtin_core import (
    _collect_project_memory_context,
    _bullet_lines,
    _collect_project_conventions_snippet,
    _extract_task_sections,
)


def _wants_chinese(language: str) -> bool:
    return str(language or "en").strip().lower().startswith("zh")


def _build_builtin_prompt(
    task: dict,
    task_file: Path,
    *,
    project_path: Path | None = None,
    review_round: int = 1,
    previous_review_feedback: str = "",
    language: str = "en",
    memory_context: str = "",
) -> str:
    """Compose the Builder prompt."""
    chinese = _wants_chinese(language)
    sections = _extract_task_sections(task.get("content") or "")
    goal = (sections.get("任务目标") or "").strip()
    acceptance = _bullet_lines(sections.get("验收标准") or "")
    builder_notes = _bullet_lines(sections.get("Builder 职责") or "")
    files = _bullet_lines(sections.get("涉及文件") or "")
    forbidden = (sections.get("禁区") or "").strip()
    dependencies = (sections.get("依赖") or "").strip()
    not_in_scope = (sections.get("不涉及") or "").strip()

    lines: list[str] = []
    if chinese:
        if review_round <= 1:
            lines.append(f"你正在执行排队任务 #{task['id']}：{task['title']}")
        else:
            lines.append(
                f"这是任务 #{task['id']} 「{task['title']}」的第 {review_round} 轮重做。"
                " 上一轮 reviewer 发现了阻塞问题，请针对性修复。"
            )
    elif review_round <= 1:
        lines.append(f"You are executing queued task #{task['id']}: {task['title']}")
    else:
        lines.append(
            f"This is implementation round {review_round} for task #{task['id']} `{task['title']}`. "
            "The previous reviewer found blocking issues; fix those findings directly."
        )
    lines.append("")

    if chinese:
        lines.append("【严格模式（必须遵守）】")
        lines.append("1. 只修改任务正文中明确列出的新建/追加/修改文件，不碰其它文件。")
        lines.append("2. 任务正文提供了代码骨架时，按骨架落地；不要新增字段/列/函数/导入/依赖。")
        lines.append("3. 不要做 reviewer 没要求的 '工程最佳实践' 扩展（如复合外键、跨币种、多账户审计等）。")
        lines.append("4. 任务没有要求跑迁移 / 拉依赖 / 启服务时，不要执行。")
        lines.append("5. 最小 diff：删代码仅限任务明确声明；保留现有 import、格式、缩进。")
    else:
        lines.append("[Strict Mode]")
        lines.append("1. Modify only files explicitly declared by the task body; do not touch unrelated files.")
        lines.append("2. If the task body provides code scaffolding, implement that scaffolding without adding extra fields, columns, functions, imports, or dependencies.")
        lines.append("3. Do not add reviewer-unspecified engineering expansions or unrelated best-practice work.")
        lines.append("4. Do not run migrations, install dependencies, or start services unless the task explicitly requires it.")
        lines.append("5. Keep the diff minimal; remove code only when the task explicitly says to do so, and preserve existing imports, formatting, and indentation.")
    lines.append("")

    if goal:
        lines.append("【任务目标】" if chinese else "[Task Goal]")
        lines.append(goal)
        lines.append("")

    if acceptance:
        lines.append("【验收标准（必须全部达成，reviewer 会逐条核对）】" if chinese else "[Acceptance Criteria: reviewer will check every item]")
        for idx, item in enumerate(acceptance, 1):
            lines.append(f"  {idx}. {item}")
        lines.append("")

    if forbidden:
        lines.append("【禁区（绝对不要碰的文件/模块）】" if chinese else "[Forbidden: files/modules you must not touch]")
        lines.append(forbidden)
        lines.append("")

    if dependencies:
        lines.append("【前置任务（已完成，直接使用其产出，不要重复实现）】" if chinese else "[Dependencies: already completed, use their output without reimplementing]")
        lines.append(dependencies)
        lines.append("")

    if not_in_scope:
        lines.append("【本任务不涉及（留给其它任务，不要提前做）】" if chinese else "[Out of Scope: leave this work for other tasks]")
        lines.append(not_in_scope)
        lines.append("")

    if builder_notes:
        lines.append("【实施提示（来自规划器）】" if chinese else "[Implementation Notes from Planner]")
        for item in builder_notes:
            lines.append(f"  - {item}")
        lines.append("")

    if files:
        lines.append("【预计要动的文件（非强制，偏离请在 Summary 说明）】" if chinese else "[Expected Files: explain any deviation in Summary]")
        for item in files:
            lines.append(f"  - {item}")
        lines.append("")

    conventions = ""
    if project_path is not None:
        conventions = _collect_project_conventions_snippet(project_path)
    if conventions:
        lines.append("【项目约定（来自 AGENTS.md / CLAUDE.md / CONTRIBUTING.md 等，必须遵守）】" if chinese else "[Project Conventions from AGENTS.md / CLAUDE.md / CONTRIBUTING.md]")
        lines.append(conventions)
        lines.append("")

        # --- Workflow toolkit: mandatory quick reference ---

    if memory_context:
        lines.append("【项目记忆上下文（自动注入，优先使用）】" if chinese else "[Project Memory Context: auto-injected, use this first]")
        lines.append(memory_context)
        lines.append("")
    lines.append("")
    lines.append("[Workflow Toolkit - MANDATORY STEPS]")
    lines.append("")
    lines.append("BEFORE writing code, you MUST execute these in order:")
    lines.append("")
    lines.append("```bash")
    lines.append("# STEP 1 - MANDATORY: Read project memory before anything else")
    lines.append("codepilot note show -p <project> --json")
    lines.append("codepilot memory events -p <project> --json")
    lines.append("codepilot wiki query -p <project> "<keyword>" --json")
    lines.append("")
    lines.append("# STEP 2 - If memory is insufficient, gather evidence")
    lines.append("codepilot explore -p <project> --prompt "<question>" --json")
    lines.append("")
    lines.append("# STEP 3 - If multi-file or high-risk, plan first")
    lines.append("codepilot plan -p <project> "<sub-goal>" --json")
    lines.append("codepilot workflow status -p <project> --json")
    lines.append("")
    lines.append("# STEP 4 - SUGGESTED: Write findings if valuable")
    lines.append("codepilot note add -p <project> \"<key finding>\"")
    lines.append("codepilot wiki add -p <project> --title \"<title>\" --body \"<body>\"")
    lines.append("")
    lines.append("# Task operations / repair:")
    lines.append("codepilot task find <keyword> -p <project> --json")
    lines.append("codepilot task stop|retry <task_id>")
    lines.append("codepilot build-fix -p <project> --task-id <task_id> --json")
    lines.append("```")
    lines.append("")
    lines.append("[HARD RULES: Step 1 (memory read) and Step 4 (memory write) is suggested. Skipping step 1 will cause review failure; step 4 is optional. Do NOT use codepilot add or codepilot go to create sub-tasks unless the task body explicitly requires it.]")
    lines.append("")
    if review_round > 1 and previous_review_feedback:
        lines.append("【上一轮 reviewer 的阻塞意见（必须处理）】" if chinese else "[Previous Reviewer Blocking Findings: must be fixed]")
        lines.append(previous_review_feedback.strip())
        lines.append("")

    lines.append(_load_prompt("builder_rules", language=language, task_file=str(task_file)).rstrip())
    return "\n".join(lines)


def _build_review_prompt(
    task: dict,
    *,
    review_round: int = 1,
    previous_findings: str = "",
    changed_files: list[str] | None = None,
    language: str = "en",
) -> str:
    """Compose the Reviewer prompt with acceptance-criteria-driven checklist."""
    chinese = _wants_chinese(language)
    sections = _extract_task_sections(task.get("content") or "")
    acceptance = _bullet_lines(sections.get("验收标准") or "")
    reviewer_notes = _bullet_lines(sections.get("Reviewer 职责") or "")
    goal = (sections.get("任务目标") or "").strip()
    forbidden = (sections.get("禁区") or "").strip()
    lines = (
        [f"请审查当前仓库中为任务 #{task['id']} `{task['title']}` 产生的未提交改动。"]
        if chinese
        else [f"Review the uncommitted changes in this repository for task #{task['id']} `{task['title']}`."]
    )

    if changed_files:
        lines.append("")
        lines.append("【本次 builder 实际改动的文件】" if chinese else "[Files changed by this builder run]")
        for path in changed_files[:40]:
            lines.append(f"  - {path}")
        if len(changed_files) > 40:
            lines.append(
                f"  - ... 共 {len(changed_files)} 个文件（截断显示前 40）"
                if chinese
                else f"  - ... {len(changed_files)} files total (showing first 40)"
            )
        if chinese:
            lines.append(
                "判定守则：如果任何一个文件不在任务【唯一目标】/【新建文件】/【追加内容】"
                "声明的路径里，即视为**越界修改**，必须判 FAIL 并在 '需要修复的点' "
                "里要求 builder 回滚那些越界改动。"
            )
        else:
            lines.append(
                "Decision rule: if any changed file is outside the task goal, files in scope, or explicitly declared additions, "
                "treat it as out-of-scope and fail with a directly actionable rollback/fix item."
            )
    if review_round > 1:
        if chinese:
            lines.append(
                f"（这是第 {review_round} 轮审查。根据 reviewer_rules 的硬约束，"
                "本轮你只能复核上一轮已经提出过的阻塞点是否修复；"
                "任何新发现的问题都放到「非阻塞观察」里，不计入 FAIL 理由。）"
            )
        else:
            lines.append(
                f"(This is review round {review_round}. Per reviewer_rules, only re-check blocking findings already raised in the previous round. "
                "Put any newly discovered issue into non-blocking observations and do not use it as a FAIL reason.)"
            )

    if goal:
        lines.append("")
        lines.append("【任务目标（审查对齐这一点，不要扩展范围）】" if chinese else "[Task Goal: review against this only, do not expand scope]")
        lines.append(goal)

    if forbidden:
        lines.append("")
        lines.append("【任务声明的禁区（若 builder 触碰则记为 FAIL）】" if chinese else "[Task-declared Forbidden Area: fail if touched]")
        lines.append(forbidden)

    if acceptance:
        lines.append("")
        lines.append("【必须逐条核对的验收标准】" if chinese else "[Acceptance Criteria: check every item]")
        for idx, item in enumerate(acceptance, 1):
            lines.append(f"  {idx}. {item}")
        lines.append(
            "对每一条，明确指出: 通过 / 未通过 / 无法判断，并说明理由（看了哪些文件或命令输出）。"
            if chinese
            else "For each item, state PASS / FAIL / N/A and give the reason, including relevant files or command output."
        )
    else:
        lines.append("")
        lines.append(
            "【没有显式验收标准】请仅按【任务目标】判断 builder 交付是否完成；"
            "不要补 reviewer 自己的额外要求、架构完整性、测试覆盖率等；"
            "任务文本之外的任何顾虑一律进「非阻塞观察」。"
            if chinese
            else "[No explicit acceptance criteria] Judge only whether the task goal is complete. Do not add your own architecture, coverage, or quality requirements; put concerns outside the task text into non-blocking observations."
        )

    if reviewer_notes:
        lines.append("")
        lines.append("【补充检查项（来自规划器的 reviewer 提示）】" if chinese else "[Additional Reviewer Notes from Planner]")
        for item in reviewer_notes:
            lines.append(f"  - {item}")

    if review_round > 1 and previous_findings.strip():
        lines.append("")
        lines.append("【上一轮给 builder 的阻塞意见（本轮只复核这些是否已修复）】" if chinese else "[Previous blocking findings given to builder: only re-check these]")
        lines.append(previous_findings.strip())

    lines.append("")
    lines.append(_load_prompt("reviewer_rules", language=language).rstrip())
    lines.append("")
    lines.append("【输出硬性要求】" if chinese else "[Hard Output Requirements]")
    if chinese:
        lines.append(
            "1. 保留 `VERDICT: PASS` 或 `VERDICT: FAIL` 单独一行，供兼容回退。\n"
            "2. 在 VERDICT 行之后追加一个 ```json 围栏块，字段包括 verdict / ac_checks / blockers / advisory，"
            "   blockers 只在 verdict=fail 时填写可直接交给 builder 的动作。"
        )
    else:
        lines.append(
            "1. Keep `VERDICT: PASS` or `VERDICT: FAIL` on its own line for compatibility fallback.\n"
            "2. After the VERDICT line, append a fenced ```json block with verdict / ac_checks / blockers / advisory. "
            "Only fill blockers when verdict=fail, and make each blocker directly actionable for the builder."
        )
    return "\n".join(lines)


def parse_review_output(review_output: str) -> ReviewerVerdict:
    """Expose the shared parser as a thin module-level symbol for consumers."""
    return parse_reviewer_output(review_output)


def _extract_review_verdict(review_output: str, reviewer_agent: str = "") -> str:
    """Legacy string-return wrapper kept for existing callers and tests."""
    return parse_reviewer_output(review_output).verdict
