"""Deterministic failure triage helpers for ``codepilot run``.

Split out from ``run.py`` to keep queue orchestration code focused while
preserving backwards-compatible wrapper entrypoints in ``run.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from codepilot.commands.reviewer_output import ReviewerVerdict, parse_reviewer_output
from codepilot.core.task_template import missing_task_template_sections


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
    """Render structured reviewer verdict items for the triage prompt.

    Returns an empty string when the verdict is missing / parse failed /
    has no actionable items. Caller stays compatible with reviewers that
    only emitted free-form text.
    """
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
    """Render the retry hint + reviewer's structured blockers as one block.

    Builder sees this appended to its task content on the next round, so the
    hint must (a) carry the AI-summarised guidance and (b) cite the reviewer's
    actual blocker / failed-AC text — otherwise builder is rerun against the
    same vague "fix it" instruction it already had.
    """
    hint_text = (retry_hint or "").strip()
    lines: list[str] = ["## AI triage 重试提示", ""]
    if hint_text:
        lines.append(f"**整体修复提示**：{hint_text}")
    else:
        lines.append("**整体修复提示**：（reviewer 反馈见下方结构化条目）")

    if reviewer_verdict is not None and reviewer_verdict.source != "empty":
        blockers = [
            str(item).strip()
            for item in (reviewer_verdict.blockers or [])
            if str(item).strip()
        ]
        advisory = [
            str(item).strip()
            for item in (reviewer_verdict.advisory or [])
            if str(item).strip()
        ]
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
                reason = _trim_triage_text(
                    str((item or {}).get("reason") or ""), limit=item_char_limit
                ) or "（无说明）"
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
            "moduleNotFoundError".lower(),
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


def _resolve_triage_project_context(
    task: dict,
    *,
    db_module,
    resolve_project_config_reference_fn: Callable[[dict], str],
    load_project_config_fn: Callable[[dict], object | None],
) -> tuple[str, str, object | None]:
    project = db_module.get_project(task["project"])
    project_path = str(task.get("project_path") or "").strip()
    config_file = str(project.get("config_file") or "").strip() if project else ""
    if not project_path and project:
        project_path = str(project.get("path") or "").strip()

    project_ref = project or {"path": project_path, "config_file": config_file}
    config_ref = resolve_project_config_reference_fn(project_ref)
    config = load_project_config_fn(project_ref) if config_ref else None
    return project_path, config_ref, config


@dataclass(frozen=True)
class _TriageEvidence:
    visible_candidates: list[dict]
    visible_candidate_ids: set[int]
    prompt: str
    project_path: str
    config_ref: str
    planner: object
    classifier_provider: str
    classifier_model: str
    api_key: str | None
    base_url: str | None
    timeout: int


def _collect_triage_evidence(
    task: dict,
    *,
    error_message: str,
    db_module,
    resolve_project_config_reference_fn: Callable[[dict], str],
    load_project_config_fn: Callable[[dict], object | None],
    resolve_planner_fn: Callable[[object | None, str], object],
    candidate_limit: int,
) -> _TriageEvidence | None:
    candidates = _iter_triage_candidates(task["project"], exclude_task_id=task["id"], db_module=db_module)
    if not candidates:
        return None

    visible_candidates = candidates[:candidate_limit]
    visible_candidate_ids = {item["id"] for item in visible_candidates}
    project_path, config_ref, config = _resolve_triage_project_context(
        task,
        db_module=db_module,
        resolve_project_config_reference_fn=resolve_project_config_reference_fn,
        load_project_config_fn=load_project_config_fn,
    )
    if not project_path:
        return None

    classifier = getattr(config, "classifier", None)
    provider_key = getattr(classifier, "provider", "") if classifier else ""
    model = getattr(classifier, "model", "") if classifier else ""
    timeout = int(getattr(classifier, "timeout", 30) or 30)
    api_key = config.get_provider_api_key(provider_key) if config and provider_key else None
    provider_cfg = config.providers.get(provider_key) if config and provider_key else None
    base_url = provider_cfg.base_url if provider_cfg else None
    planner = resolve_planner_fn(config, "automation")
    prompt = _build_deterministic_failure_triage_prompt(
        task,
        error_message=error_message,
        candidates=visible_candidates,
        candidate_limit=candidate_limit,
    )

    return _TriageEvidence(
        visible_candidates=visible_candidates,
        visible_candidate_ids=visible_candidate_ids,
        prompt=prompt,
        project_path=project_path,
        config_ref=config_ref,
        planner=planner,
        classifier_provider=provider_key,
        classifier_model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
    )


def _classify_triage_action(
    evidence: _TriageEvidence,
    *,
    schema: dict,
    call_structured_fn,
    gateway_request_cls,
) -> tuple[dict, str] | None:
    try:
        response = call_structured_fn(
            gateway_request_cls(
                prompt=evidence.prompt,
                schema=schema,
                classifier_provider=evidence.classifier_provider,
                classifier_model=evidence.classifier_model,
                api_key=evidence.api_key,
                base_url=evidence.base_url,
                project_path=evidence.project_path,
                config_ref=evidence.config_ref,
                planner=evidence.planner,
                timeout=max(evidence.timeout, 15),
            )
        )
    except Exception:
        return None

    if not response.ok or not isinstance(response.payload, dict):
        return None
    return response.payload, str(getattr(response, "source", "") or "")


def _collect_review_failure_evidence(
    task: dict,
    *,
    error_message: str,
    review_output: str,
    builder_output: str,
    db_module,
    resolve_project_config_reference_fn: Callable[[dict], str],
    load_project_config_fn: Callable[[dict], object | None],
    resolve_planner_fn: Callable[[object | None, str], object],
    candidate_limit: int,
) -> _TriageEvidence | None:
    candidates = _iter_triage_candidates(task["project"], exclude_task_id=task["id"], db_module=db_module)
    visible_candidates = candidates[:candidate_limit]
    visible_candidate_ids = {item["id"] for item in visible_candidates}
    project_path, config_ref, config = _resolve_triage_project_context(
        task,
        db_module=db_module,
        resolve_project_config_reference_fn=resolve_project_config_reference_fn,
        load_project_config_fn=load_project_config_fn,
    )
    if not project_path:
        return None

    classifier = getattr(config, "classifier", None)
    provider_key = getattr(classifier, "provider", "") if classifier else ""
    model = getattr(classifier, "model", "") if classifier else ""
    timeout = int(getattr(classifier, "timeout", 30) or 30)
    api_key = config.get_provider_api_key(provider_key) if config and provider_key else None
    provider_cfg = config.providers.get(provider_key) if config and provider_key else None
    base_url = provider_cfg.base_url if provider_cfg else None
    planner = resolve_planner_fn(config, "automation")
    # Parse the reviewer transcript so blockers / advisory / failed AC items
    # become first-class signals in the triage prompt — replan_content can
    # reference specific reviewer findings instead of guessing from prose.
    try:
        reviewer_verdict = parse_reviewer_output(review_output or "")
    except Exception:
        reviewer_verdict = None
    prompt = _build_review_failure_triage_prompt(
        task,
        error_message=error_message,
        review_output=review_output,
        builder_output=builder_output,
        candidates=visible_candidates,
        candidate_limit=candidate_limit,
        reviewer_verdict=reviewer_verdict,
    )

    return _TriageEvidence(
        visible_candidates=visible_candidates,
        visible_candidate_ids=visible_candidate_ids,
        prompt=prompt,
        project_path=project_path,
        config_ref=config_ref,
        planner=planner,
        classifier_provider=provider_key,
        classifier_model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
    )


def _map_merge_decision(
    task: dict,
    *,
    error_message: str,
    payload: dict,
    source: str,
    visible_candidate_ids: set[int],
    db_module,
) -> dict | None:
    matched_task_id = payload.get("matched_task_id")
    if not (isinstance(matched_task_id, int) and matched_task_id in visible_candidate_ids):
        return None

    target = db_module.get_task(matched_task_id)
    if not target or target.get("status") not in {"backlog", "in_progress"}:
        return None

    rationale = str(payload.get("rationale") or "").strip()
    merged_note = str(payload.get("merged_note") or "").strip()
    merge_note = _format_triage_merge_note(
        task,
        error_message=error_message,
        rationale=rationale,
        merged_note=merged_note,
    )
    db_module.update_task(target["id"], content=_append_task_content(target.get("content") or "", merge_note))
    return {
        "action": "merge",
        "matched_task_id": target["id"],
        "matched_task_title": target.get("title", ""),
        "rationale": rationale,
        "note": merge_note,
        "source": source,
    }


def _map_retry_with_hint_decision(
    task: dict,
    *,
    error_message: str,
    payload: dict,
    source: str,
) -> dict | None:
    if payload.get("matched_task_id") is not None:
        return None
    rationale = str(payload.get("rationale") or "").strip()
    retry_hint = str(payload.get("retry_hint") or "").strip()
    if not retry_hint:
        return None
    note = _format_retry_hint_note(
        task,
        error_message=error_message,
        rationale=rationale,
        retry_hint=retry_hint,
    )
    return {
        "action": "retry_with_hint",
        "matched_task_id": None,
        "matched_task_title": "",
        "rationale": rationale,
        "retry_hint": retry_hint,
        "note": note,
        "source": source,
    }


def _map_replan_decision(
    task: dict,
    *,
    error_message: str,
    payload: dict,
    source: str,
) -> dict | None:
    if payload.get("matched_task_id") is not None:
        return None
    rationale = str(payload.get("rationale") or "").strip()
    replan_title = str(payload.get("replan_title") or "").strip()
    replan_content = str(payload.get("replan_content") or "").strip()
    if not replan_title or not replan_content:
        return None
    final_content = _format_replan_task_content(
        task,
        error_message=error_message,
        rationale=rationale,
        replan_content=replan_content,
    )
    return {
        "action": "replan",
        "matched_task_id": None,
        "matched_task_title": "",
        "rationale": rationale,
        "replan_title": replan_title,
        "replan_content": final_content,
        "note": _trim_triage_text(rationale or replan_title, limit=240),
        "source": source,
    }


def _map_discard_decision(payload: dict, *, source: str) -> dict | None:
    if payload.get("matched_task_id") is not None:
        return None
    rationale = str(payload.get("rationale") or "").strip()
    merged_note = str(payload.get("merged_note") or "").strip()
    return {
        "action": "discard",
        "matched_task_id": None,
        "matched_task_title": "",
        "rationale": rationale,
        "note": _trim_triage_text(merged_note or rationale, limit=240),
        "source": source,
    }


def _map_triage_decision(
    task: dict,
    *,
    error_message: str,
    payload: dict,
    source: str,
    visible_candidate_ids: set[int],
    db_module,
) -> dict | None:
    action = str(payload.get("action") or "").strip().lower()
    if action == "merge":
        return _map_merge_decision(
            task,
            error_message=error_message,
            payload=payload,
            source=source,
            visible_candidate_ids=visible_candidate_ids,
            db_module=db_module,
        )
    if action == "discard":
        return _map_discard_decision(payload, source=source)
    return None


def _map_review_failure_decision(
    task: dict,
    *,
    error_message: str,
    payload: dict,
    source: str,
    visible_candidate_ids: set[int],
    db_module,
) -> dict | None:
    action = str(payload.get("action") or "").strip().lower()
    if action in {"merge", "merge_partial"}:
        decision = _map_merge_decision(
            task,
            error_message=error_message,
            payload=payload,
            source=source,
            visible_candidate_ids=visible_candidate_ids,
            db_module=db_module,
        )
        if decision:
            decision["action"] = "merge_partial"
        return decision
    if action == "discard":
        return _map_discard_decision(payload, source=source)
    if action == "retry_with_hint":
        return _map_retry_with_hint_decision(
            task,
            error_message=error_message,
            payload=payload,
            source=source,
        )
    if action == "replan":
        return _map_replan_decision(
            task,
            error_message=error_message,
            payload=payload,
            source=source,
        )
    return None


def triage_deterministic_failure(
    task: dict,
    error_message: str,
    *,
    db_module,
    resolve_project_config_reference_fn: Callable[[dict], str],
    load_project_config_fn: Callable[[dict], object | None],
    resolve_planner_fn: Callable[[object | None, str], object],
    call_structured_fn,
    gateway_request_cls,
    candidate_limit: int = _DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
) -> dict | None:
    evidence = _collect_triage_evidence(
        task,
        error_message=error_message,
        db_module=db_module,
        resolve_project_config_reference_fn=resolve_project_config_reference_fn,
        load_project_config_fn=load_project_config_fn,
        resolve_planner_fn=resolve_planner_fn,
        candidate_limit=candidate_limit,
    )
    if not evidence:
        return None

    classified = _classify_triage_action(
        evidence,
        schema=_DETERMINISTIC_FAILURE_TRIAGE_SCHEMA,
        call_structured_fn=call_structured_fn,
        gateway_request_cls=gateway_request_cls,
    )
    if not classified:
        return None

    payload, source = classified
    return _map_triage_decision(
        task,
        error_message=error_message,
        payload=payload,
        source=source,
        visible_candidate_ids=evidence.visible_candidate_ids,
        db_module=db_module,
    )


def triage_review_failure(
    task: dict,
    error_message: str,
    *,
    review_output: str,
    builder_output: str,
    db_module,
    resolve_project_config_reference_fn: Callable[[dict], str],
    load_project_config_fn: Callable[[dict], object | None],
    resolve_planner_fn: Callable[[object | None, str], object],
    call_structured_fn,
    gateway_request_cls,
    candidate_limit: int = _DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
) -> dict | None:
    evidence = _collect_review_failure_evidence(
        task,
        error_message=error_message,
        review_output=review_output,
        builder_output=builder_output,
        db_module=db_module,
        resolve_project_config_reference_fn=resolve_project_config_reference_fn,
        load_project_config_fn=load_project_config_fn,
        resolve_planner_fn=resolve_planner_fn,
        candidate_limit=candidate_limit,
    )
    if not evidence:
        return None

    classified = _classify_triage_action(
        evidence,
        schema=_REVIEW_FAILURE_TRIAGE_SCHEMA,
        call_structured_fn=call_structured_fn,
        gateway_request_cls=gateway_request_cls,
    )
    if not classified:
        return None

    payload, source = classified
    return _map_review_failure_decision(
        task,
        error_message=error_message,
        payload=payload,
        source=source,
        visible_candidate_ids=evidence.visible_candidate_ids,
        db_module=db_module,
    )


def apply_deterministic_failure_triage(
    task: dict,
    error_message: str,
    *,
    triage_fn,
) -> str:
    decision = triage_fn(task, error_message)
    if not decision:
        return error_message

    if decision["action"] == "merge":
        triage_line = (
            f"AI triage: 已归并到 #{decision['matched_task_id']} "
            f"{decision['matched_task_title']}（{decision.get('rationale') or '已有待办覆盖'}）"
        )
    else:
        triage_line = f"AI triage: 已丢弃单独跟进（{decision.get('rationale') or '无需额外待办'}）"
    return f"{error_message}\n{triage_line}".strip()


def _fallback_review_failure_result(
    task: dict,
    error_message: str,
    *,
    mark_task_failed_fn,
    handle_failure_fn,
    retry_on_failure: bool,
    stop_on_failure: bool,
) -> dict:
    if retry_on_failure:
        updated, should_stop = handle_failure_fn(
            task,
            error_message,
            stop_on_failure=stop_on_failure,
        )
    else:
        updated = mark_task_failed_fn(task, error_message)
        should_stop = bool(stop_on_failure)
    return {
        "updated": updated,
        "error_message": error_message,
        "decision": None,
        "should_stop": should_stop,
    }


def _parse_reviewer_verdict_safe(review_output: str) -> Optional[ReviewerVerdict]:
    try:
        return parse_reviewer_output(review_output or "")
    except Exception:
        return None


def _override_review_failure_discard_decision(
    decision: dict,
    *,
    reviewer_verdict: Optional[ReviewerVerdict],
    review_output: str,
    error_message: str,
) -> dict:
    action = str(decision.get("action") or "").strip().lower()
    if action != "discard":
        return decision
    if not _reviewer_actionable_items(reviewer_verdict):
        return decision
    if not _review_output_has_actionable_failure(review_output, error_message):
        return decision

    rationale = str(decision.get("rationale") or "").strip()
    retry_hint = _build_forced_retry_hint(
        reviewer_verdict=reviewer_verdict,
        review_output=review_output,
        error_message=error_message,
        rationale=rationale,
    )
    return {
        **decision,
        "action": "retry_with_hint",
        "retry_hint": retry_hint,
        "rationale": rationale or "reviewer 已给出可执行修复点，不能丢弃",
        "overridden_from": "discard",
        "override_reason": "reviewer_actionable_failure",
    }


def _apply_retry_with_hint_decision(
    task: dict,
    error_message: str,
    *,
    decision: dict,
    reviewer_verdict: Optional[ReviewerVerdict],
    mark_task_failed_fn,
    handle_failure_fn,
    db_module,
    retry_on_failure: bool,
    stop_on_failure: bool,
) -> dict:
    rationale = str(decision.get("rationale") or "").strip()

    if not retry_on_failure:
        if decision.get("overridden_from") == "discard":
            triage_line = (
                "AI triage: reviewer 有明确修复点，但当前运行已禁用自动重试，"
                f"任务标记为失败（{rationale or '按 reviewer 反馈终止'}）"
            )
        else:
            triage_line = (
                "AI triage: 建议追加提示后重试，但当前运行已禁用自动重试，"
                f"任务标记为失败（{rationale or 'review 反馈可继续修复'}）"
            )
        final_error = f"{error_message}\n{triage_line}".strip()
        updated = mark_task_failed_fn(task, final_error)
        return {
            "updated": updated,
            "error_message": final_error,
            "decision": decision,
            "should_stop": bool(stop_on_failure),
        }

    updated, retry_should_stop = handle_failure_fn(
        task,
        error_message,
        stop_on_failure=stop_on_failure,
    )
    if updated["status"] == "failed":
        triage_line = f"AI triage: 建议自动重试，但已达到重试上限（{rationale or 'review 反馈可继续修复'}）"
    elif decision.get("overridden_from") == "discard":
        triage_line = f"AI triage: reviewer 有明确修复点，已覆盖 discard 并回退 backlog（{rationale or '按 reviewer 反馈重试'}）"
    else:
        triage_line = f"AI triage: 已追加重试提示并回退 backlog（{rationale or '按 reviewer 反馈重试'}）"
    final_error = f"{error_message}\n{triage_line}".strip()

    retry_hint = str(decision.get("retry_hint") or "").strip()
    hint_block = _format_retry_hint_block(retry_hint, reviewer_verdict)
    updated = db_module.update_task(
        task["id"],
        content=_append_task_content(task.get("content") or "", hint_block),
        error_message=final_error,
    )
    decision["applied_hint_block"] = hint_block
    return {
        "updated": updated,
        "error_message": final_error,
        "decision": decision,
        "should_stop": retry_should_stop or updated["status"] == "failed",
    }


def _apply_replan_decision(
    task: dict,
    error_message: str,
    *,
    decision: dict,
    rationale: str,
    mark_task_failed_fn,
    db_module,
    terminal_should_stop: bool,
) -> dict:
    replan_title = str(decision.get("replan_title") or "").strip()
    replan_content = str(decision.get("replan_content") or "").strip()
    missing_sections = (
        missing_task_template_sections(replan_content)
        if replan_content else
        ["（replan_content 为空）"]
    )
    if not replan_title or missing_sections:
        triage_line = (
            "AI triage: 建议 replan，但 replan_content 不符合 task-template "
            f"（缺章节: {', '.join(missing_sections) if missing_sections else '无标题'}），"
            f"已降级为 discard（{rationale or '当前任务需要重新规划'}）"
        )
        final_error = f"{error_message}\n{triage_line}".strip()
        updated = mark_task_failed_fn(task, final_error)
        decision["downgraded_to"] = "discard"
        decision["downgrade_reason"] = "replan_content 模板不合规"
        return {
            "updated": updated,
            "error_message": final_error,
            "decision": decision,
            "should_stop": terminal_should_stop,
        }

    followup = db_module.create_task(
        task["project"],
        replan_title,
        content=replan_content,
        agent=task.get("agent") or "dual",
        priority=task.get("priority") or "P2",
        project_path=task.get("project_path") or None,
        max_retries=int(task.get("max_retries") or 3),
        source="system",
        fallback_reason=f"ai_triage_replan:{task.get('id')}",
    )
    decision["created_task_id"] = followup["id"]
    decision["created_task_title"] = followup["title"]
    triage_line = (
        f"AI triage: 已转成新任务 #{followup['id']} "
        f"{followup['title']}（{rationale or '当前任务需要重新规划'}）"
    )
    final_error = f"{error_message}\n{triage_line}".strip()
    updated = mark_task_failed_fn(task, final_error)
    return {
        "updated": updated,
        "error_message": final_error,
        "decision": decision,
        "should_stop": terminal_should_stop,
    }


def _apply_terminal_review_failure_decision(
    task: dict,
    error_message: str,
    *,
    decision: dict,
    rationale: str,
    mark_task_failed_fn,
    terminal_should_stop: bool,
) -> dict:
    if decision["action"] == "merge_partial":
        triage_line = (
            f"AI triage: 已归并到 #{decision['matched_task_id']} "
            f"{decision['matched_task_title']}（{rationale or '已有待办覆盖'}）"
        )
    else:
        triage_line = f"AI triage: 已丢弃单独跟进（{rationale or '无需额外待办'}）"
    final_error = f"{error_message}\n{triage_line}".strip()
    updated = mark_task_failed_fn(task, final_error)
    return {
        "updated": updated,
        "error_message": final_error,
        "decision": decision,
        "should_stop": terminal_should_stop,
    }


def apply_review_failure_triage(
    task: dict,
    error_message: str,
    *,
    review_output: str,
    builder_output: str,
    triage_fn,
    mark_task_failed_fn,
    handle_failure_fn,
    db_module,
    retry_on_failure: bool = False,
    stop_on_failure: bool = True,
) -> dict:
    """Run AI triage on a review-stage failure and apply the decision.

    When the AI gateway is unavailable (returns None) the helper falls back
    to the legacy behaviour controlled by ``retry_on_failure`` so existing
    runs keep retrying / failing exactly as before. Returned dict shape:

    ``{"updated": <task row>, "error_message": <annotated text>,
       "decision": <triage decision dict or None>,
       "should_stop": <bool>}``

    ``should_stop`` lets the orchestrator decide whether to break the run
    loop without re-deriving the retry state.
    """
    decision = triage_fn(task, error_message, review_output=review_output, output=builder_output)
    if not decision:
        return _fallback_review_failure_result(
            task,
            error_message,
            mark_task_failed_fn=mark_task_failed_fn,
            handle_failure_fn=handle_failure_fn,
            retry_on_failure=retry_on_failure,
            stop_on_failure=stop_on_failure,
        )

    # Re-parse reviewer output once so retry_with_hint / replan paths can quote
    # structured blockers; tolerated to be ``None`` for legacy reviewers that
    # never emitted a JSON fence.
    reviewer_verdict = _parse_reviewer_verdict_safe(review_output)

    decision = _override_review_failure_discard_decision(
        decision,
        reviewer_verdict=reviewer_verdict,
        review_output=review_output,
        error_message=error_message,
    )
    action = decision["action"]
    rationale = str(decision.get("rationale") or "").strip()

    if action == "retry_with_hint":
        return _apply_retry_with_hint_decision(
            task,
            error_message,
            decision=decision,
            reviewer_verdict=reviewer_verdict,
            mark_task_failed_fn=mark_task_failed_fn,
            handle_failure_fn=handle_failure_fn,
            db_module=db_module,
            retry_on_failure=retry_on_failure,
            stop_on_failure=stop_on_failure,
        )

    # 所有终态分支（replan / merge_partial / discard / replan-downgrade）的
    # ``should_stop`` 都跟随 stop_on_failure：
    # - builtin 执行器传 True → 单任务失败立即停 run loop（符合既有语义）
    # - dispatch 执行器传 False → 单任务失败但 run loop 继续跑下一条
    # 之前硬编码 True 会让 dispatch 模式被 AI triage 意外放大成"整轮停止"。
    terminal_should_stop = bool(stop_on_failure)

    if action == "replan":
        return _apply_replan_decision(
            task,
            error_message,
            decision=decision,
            rationale=rationale,
            mark_task_failed_fn=mark_task_failed_fn,
            db_module=db_module,
            terminal_should_stop=terminal_should_stop,
        )

    return _apply_terminal_review_failure_decision(
        task,
        error_message,
        decision=decision,
        rationale=rationale,
        mark_task_failed_fn=mark_task_failed_fn,
        terminal_should_stop=terminal_should_stop,
    )

