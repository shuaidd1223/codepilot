"""Prompt and text-formatting helpers for run failure triage."""

from __future__ import annotations

import re
from typing import Optional

from codepilot.commands.reviewer_output import ReviewerVerdict


_DETERMINISTIC_FAILURE_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["merge", "discard"]},
        "matched_task_id": {"type": ["integer", "null"]},
        "rationale": {"type": "string"},
        "merged_note": {"type": "string"},
    },
    "required": ["action", "matched_task_id", "rationale", "merged_note"],
    "additionalProperties": False,
}

_DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT = 20

_REVIEW_FAILURE_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["discard", "retry_with_hint", "replan", "merge_partial", "merge"]},
        "matched_task_id": {"type": ["integer", "null"]},
        "rationale": {"type": "string"},
        "merged_note": {"type": "string"},
        "retry_hint": {"type": "string"},
        "replan_title": {"type": "string"},
        "replan_content": {"type": "string"},
    },
    "required": [
        "action",
        "matched_task_id",
        "rationale",
        "merged_note",
        "retry_hint",
        "replan_title",
        "replan_content",
    ],
    "additionalProperties": False,
}


def _trim_triage_text(text: str, limit: int = 280) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _iter_triage_candidates(project: str, *, exclude_task_id: int, db_module) -> list[dict]:
    return [
        task
        for task in db_module.list_tasks(project=project)
        if task.get("id") != exclude_task_id and task.get("status") in {"backlog", "in_progress"}
    ]


def _build_deterministic_failure_triage_prompt(
    task: dict,
    *,
    error_message: str,
    candidates: list[dict],
    candidate_limit: int = _DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
) -> str:
    current_content = _trim_triage_text(task.get("content") or "", limit=500) or "（无）"
    current_error = _trim_triage_text(error_message, limit=500) or "（无）"
    candidate_lines: list[str] = []
    for candidate in candidates[:candidate_limit]:
        candidate_lines.append(
            "\n".join(
                [
                    f"- #{candidate['id']} [{candidate.get('status')}] {candidate.get('title', '')}",
                    f"  content: {_trim_triage_text(candidate.get('content') or '', limit=220) or '（无）'}",
                    f"  latest: {_trim_triage_text(candidate.get('error_message') or candidate.get('delivery_record') or '', limit=180) or '（无）'}",
                ]
            )
        )

    candidate_block = "\n".join(candidate_lines) if candidate_lines else "（无可归并候选）"
    return (
        "你在做 deterministic failure 的 AI triage。\n"
        "目标只有两个动作：\n"
        "1. merge：当前失败任务本质上已经被某个 backlog/in_progress 任务覆盖，应把失败上下文归并到那个任务。\n"
        "2. discard：当前失败不需要生成新的待办，也不该归并到任何现有任务。\n\n"
        "严格规则：\n"
        "- 只能返回 merge 或 discard。\n"
        "- 只有在候选任务明确覆盖当前失败问题时才能 merge。\n"
        "- matched_task_id 只能填候选列表里的任务 id；discard 时必须为 null。\n"
        "- merged_note 写成会追加到目标任务里的中文简述；discard 时写一句简短处置说明。\n"
        "- 不要建议 retry，不要建议新建任务，不要输出 JSON 以外的内容。\n\n"
        f"当前失败任务:\n"
        f"- id: {task.get('id')}\n"
        f"- title: {task.get('title', '')}\n"
        f"- priority: {task.get('priority', '')}\n"
        f"- content: {current_content}\n"
        f"- failure: {current_error}\n\n"
        "现有开放任务候选:\n"
        f"{candidate_block}\n\n"
        '输出 JSON: {"action":"merge|discard","matched_task_id":123|null,"rationale":"...","merged_note":"..."}'
    )


def _format_reviewer_verdict_block(
    reviewer_verdict: Optional[ReviewerVerdict],
    *,
    item_limit: int = 6,
    item_char_limit: int = 160,
) -> str:
    """Render structured reviewer verdict items for the triage prompt."""
    if reviewer_verdict is None or reviewer_verdict.source == "empty":
        return ""

    blockers = [str(item).strip() for item in (reviewer_verdict.blockers or []) if str(item).strip()]
    advisory = [str(item).strip() for item in (reviewer_verdict.advisory or []) if str(item).strip()]
    failed_acs = [
        item for item in (reviewer_verdict.ac_checks or [])
        if str((item or {}).get("status") or "").strip().lower() == "fail"
    ]
    if not blockers and not advisory and not failed_acs:
        return ""

    lines: list[str] = ["reviewer 结构化反馈:"]
    lines.append(f"- verdict: {reviewer_verdict.verdict} (source={reviewer_verdict.source})")
    if blockers:
        lines.append("- 阻塞项 blockers (必须在 replan_content / retry_hint 中明确引用):")
        for idx, item in enumerate(blockers[:item_limit], start=1):
            lines.append(f"  B{idx}. {_trim_triage_text(item, limit=item_char_limit)}")
        if len(blockers) > item_limit:
            lines.append(f"  ... 还有 {len(blockers) - item_limit} 条 blocker 未列出。")
    if failed_acs:
        lines.append("- 验收失败项 ac_checks(status=fail):")
        for idx, item in enumerate(failed_acs[:item_limit], start=1):
            ac_id = str((item or {}).get("id") or f"AC{idx}")
            reason = _trim_triage_text(str((item or {}).get("reason") or ""), limit=item_char_limit) or "（无说明）"
            lines.append(f"  - {ac_id}: {reason}")
    if advisory:
        lines.append("- 提醒项 advisory (非阻塞):")
        for idx, item in enumerate(advisory[:item_limit], start=1):
            lines.append(f"  A{idx}. {_trim_triage_text(item, limit=item_char_limit)}")
    return "\n".join(lines)


def _build_review_failure_triage_prompt(
    task: dict,
    *,
    error_message: str,
    review_output: str,
    builder_output: str,
    candidates: list[dict],
    candidate_limit: int = _DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
    reviewer_verdict: Optional[ReviewerVerdict] = None,
) -> str:
    current_content = _trim_triage_text(task.get("content") or "", limit=500) or "（无）"
    current_error = _trim_triage_text(error_message, limit=500) or "（无）"
    review_summary = _trim_triage_text(review_output, limit=500) or "（无）"
    builder_summary = _trim_triage_text(builder_output, limit=400) or "（无）"
    reviewer_block = _format_reviewer_verdict_block(reviewer_verdict)
    candidate_lines: list[str] = []
    for candidate in candidates[:candidate_limit]:
        candidate_lines.append(
            "\n".join(
                [
                    f"- #{candidate['id']} [{candidate.get('status')}] {candidate.get('title', '')}",
                    f"  content: {_trim_triage_text(candidate.get('content') or '', limit=220) or '（无）'}",
                    f"  latest: {_trim_triage_text(candidate.get('error_message') or candidate.get('delivery_record') or '', limit=180) or '（无）'}",
                ]
            )
        )

    candidate_block = "\n".join(candidate_lines) if candidate_lines else "（无开放候选任务）"
    reviewer_section = f"\n{reviewer_block}\n" if reviewer_block else ""
    grounded_rule = (
        "- replan 与 retry_with_hint 时，replan_content / retry_hint 必须**明确引用** reviewer "
        "结构化反馈中的至少一条 blocker（用 B1/B2 编号或原文片段），不能凭空规划；"
        "如果上面没有结构化 blockers，再退化到引用 review_output 中的具体片段。\n"
    ) if reviewer_block else ""
    return (
        "你在做 review failure 的 AI triage。\n"
        "当前任务已经跑完 builder/reviewer 闭环，但 reviewer 最终仍未通过。"
        "你只能基于任务说明、review 输出、builder 摘要、现有开放任务候选来决定下一步；"
        "系统会按你的动作决定保留现场重试，还是终态清理后继续队列。\n\n"
        "可选动作只有四种：\n"
        "1. retry_with_hint：当前任务仍然成立，且 reviewer 给了可执行修复点；把反馈浓缩成明确 hint 后重跑同一个任务。\n"
        "2. replan：当前任务粒度/路径/目标错了，应该生成一个新的 backlog 任务重新规划。\n"
        "3. merge_partial：当前失败里有一部分价值已经被别的 backlog/in_progress 任务覆盖，应把失败上下文归并到那个任务。\n"
        "4. discard：只有在当前失败明确不需要继续跟进、需求已经无效、或没有任何可执行修复价值时才允许丢弃。\n\n"
        "严格规则：\n"
        "- 只能返回 discard / retry_with_hint / replan / merge_partial；兼容旧字段时 merge 视为 merge_partial。\n"
        "- 只要 reviewer 输出里有具体 blocker、失败 AC、缺文件/缺导入/测试失败等可执行修复点，且任务目标仍有效，禁止 discard；优先 retry_with_hint。\n"
        "- discard / retry_with_hint / replan 时 matched_task_id 必须为 null。\n"
        "- merge_partial 时 matched_task_id 必须是候选列表中的任务 id。\n"
        "- retry_with_hint 时 retry_hint 必须是可直接追加到任务里的中文修复提示；其他动作的 retry_hint 置空。\n"
        "- replan 时必须给出 replan_title 和 replan_content；其他动作这两个字段置空。\n"
        "- merge_partial 时 merged_note 必须写成会追加到目标任务里的中文简述；discard 时可写一句简短处置说明；retry/replan 时可留空。\n"
        f"{grounded_rule}"
        "- 不要输出 JSON 以外的内容。\n\n"
        f"当前失败任务:\n"
        f"- id: {task.get('id')}\n"
        f"- title: {task.get('title', '')}\n"
        f"- priority: {task.get('priority', '')}\n"
        f"- content: {current_content}\n"
        f"- failure: {current_error}\n"
        f"- review_output: {review_summary}\n"
        f"- builder_output: {builder_summary}\n"
        f"{reviewer_section}\n"
        "现有开放任务候选:\n"
        f"{candidate_block}\n\n"
        '输出 JSON: {"action":"discard|retry_with_hint|replan|merge_partial","matched_task_id":123|null,"rationale":"...","merged_note":"...","retry_hint":"...","replan_title":"...","replan_content":"..."}'
    )


def _format_triage_merge_note(
    source_task: dict,
    *,
    error_message: str,
    rationale: str,
    merged_note: str,
) -> str:
    detail = _trim_triage_text(merged_note or rationale, limit=240) or "已归并失败上下文。"
    failure_head = _trim_triage_text(error_message, limit=240) or "（无）"
    return "\n".join(
        [
            "## AI triage 归并记录",
            "",
            f"- 来源任务: #{source_task.get('id')} {source_task.get('title', '')}",
            f"- 失败摘要: {failure_head}",
            f"- 归并说明: {detail}",
        ]
    )


def _format_retry_hint_note(
    source_task: dict,
    *,
    error_message: str,
    rationale: str,
    retry_hint: str,
) -> str:
    detail = _trim_triage_text(retry_hint or rationale, limit=320) or "根据 reviewer 反馈聚焦修复。"
    failure_head = _trim_triage_text(error_message, limit=240) or "（无）"
    return "\n".join(
        [
            "## AI triage 重试提示",
            "",
            f"- 来源任务: #{source_task.get('id')} {source_task.get('title', '')}",
            f"- 失败摘要: {failure_head}",
            f"- 重试提示: {detail}",
        ]
    )


def _format_replan_task_content(
    source_task: dict,
    *,
    error_message: str,
    rationale: str,
    replan_content: str,
) -> str:
    base_content = (replan_content or "").strip() or "## 任务目标\n\n根据 reviewer 失败重新规划。"
    followup_note = "\n".join(
        [
            "## AI triage 来源",
            "",
            f"- 来源任务: #{source_task.get('id')} {source_task.get('title', '')}",
            f"- 失败摘要: {_trim_triage_text(error_message, limit=240) or '（无）'}",
            f"- triage 理由: {_trim_triage_text(rationale, limit=240) or 'review 失败需要重新规划'}",
        ]
    )
    return _append_task_content(base_content, followup_note)


def _append_task_content(base: str, note: str) -> str:
    base = (base or "").rstrip()
    note = (note or "").strip()
    if not base:
        return note
    if not note:
        return base
    return f"{base}\n\n{note}"


def _format_retry_hint_block(
    retry_hint: str,
    reviewer_verdict: Optional[ReviewerVerdict],
    *,
    item_limit: int = 6,
    item_char_limit: int = 220,
) -> str:
    """Render the retry hint + reviewer's structured blockers as one block."""
    hint_text = (retry_hint or "").strip()
    lines: list[str] = ["## AI triage 重试提示", ""]
    if hint_text:
        lines.append(f"**整体修复提示**：{hint_text}")
    else:
        lines.append("**整体修复提示**：（reviewer 反馈见下方结构化条目）")

    if reviewer_verdict is not None and reviewer_verdict.source != "empty":
        blockers = [str(item).strip() for item in (reviewer_verdict.blockers or []) if str(item).strip()]
        advisory = [str(item).strip() for item in (reviewer_verdict.advisory or []) if str(item).strip()]
        failed_acs = [
            item for item in (reviewer_verdict.ac_checks or [])
            if str((item or {}).get("status") or "").strip().lower() == "fail"
        ]
        if blockers:
            lines.append("")
            lines.append("**必须解决的 blockers**（按 reviewer 上一轮的原文）：")
            for idx, item in enumerate(blockers[:item_limit], start=1):
                lines.append(f"- B{idx}. {_trim_triage_text(item, limit=item_char_limit)}")
            if len(blockers) > item_limit:
                lines.append(f"- ... 另有 {len(blockers) - item_limit} 条 blocker 未列出。")
        if failed_acs:
            lines.append("")
            lines.append("**未通过的 AC 项**：")
            for idx, item in enumerate(failed_acs[:item_limit], start=1):
                ac_id = str((item or {}).get("id") or f"AC{idx}")
                reason = _trim_triage_text(str((item or {}).get("reason") or ""), limit=item_char_limit) or "（无说明）"
                lines.append(f"- {ac_id}: {reason}")
        if advisory:
            lines.append("")
            lines.append("**advisory（参考，非阻塞）**：")
            for idx, item in enumerate(advisory[:item_limit], start=1):
                lines.append(f"- A{idx}. {_trim_triage_text(item, limit=item_char_limit)}")
    return "\n".join(lines).rstrip()


def _reviewer_actionable_items(
    reviewer_verdict: Optional[ReviewerVerdict],
    *,
    item_limit: int = 6,
    item_char_limit: int = 220,
) -> list[str]:
    if reviewer_verdict is None or reviewer_verdict.source == "empty":
        return []

    items: list[str] = []
    for item in reviewer_verdict.blockers or []:
        text = str(item).strip()
        if text:
            items.append(_trim_triage_text(text, limit=item_char_limit))

    for idx, item in enumerate(reviewer_verdict.ac_checks or [], start=1):
        if str((item or {}).get("status") or "").strip().lower() != "fail":
            continue
        ac_id = str((item or {}).get("id") or f"AC{idx}")
        reason = _trim_triage_text(str((item or {}).get("reason") or ""), limit=item_char_limit)
        items.append(f"{ac_id}: {reason or '验收项未通过'}")

    return items[:item_limit]


def _review_output_has_actionable_failure(review_output: str, error_message: str) -> bool:
    text = f"{review_output or ''}\n{error_message or ''}".lower()
    return any(
        marker in text
        for marker in (
            "verdict: fail",
            '"verdict":"fail"',
            '"status":"fail"',
            "modulenotfounderror",
            "importerror",
            "assertionerror",
            "failed",
            "缺少",
            "未通过",
            "不存在",
        )
    )


def _build_forced_retry_hint(
    *,
    reviewer_verdict: Optional[ReviewerVerdict],
    review_output: str,
    error_message: str,
    rationale: str,
) -> str:
    items = _reviewer_actionable_items(reviewer_verdict)
    if items:
        return "reviewer 已给出明确阻塞点，不能丢弃；请按以下事项继续修复：" + "；".join(items)
    compact_review = _trim_triage_text(review_output or error_message, limit=420)
    if compact_review:
        return f"reviewer 仍有可执行失败反馈，不能丢弃；请围绕这段反馈继续修复：{compact_review}"
    return rationale or "reviewer 未通过，继续修复当前任务而不是丢弃。"
