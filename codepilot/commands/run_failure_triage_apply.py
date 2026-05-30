"""Decision application helpers for run failure triage."""

from __future__ import annotations

from typing import Optional

from codepilot.commands.reviewer_output import ReviewerVerdict, parse_reviewer_output
from codepilot.commands.run_failure_triage_prompts import (
    _append_task_content,
    _build_forced_retry_hint,
    _format_retry_hint_block,
    _review_output_has_actionable_failure,
    _reviewer_actionable_items,
)
from codepilot.core.task_template import missing_task_template_sections


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
    missing_sections = missing_task_template_sections(replan_content) if replan_content else ["（replan_content 为空）"]
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
    """Run AI triage on a review-stage failure and apply the decision."""
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
