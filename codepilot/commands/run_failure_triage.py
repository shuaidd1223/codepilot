"""Deterministic failure triage helpers for ``codepilot run``.

Split out from ``run.py`` to keep queue orchestration code focused while
preserving backwards-compatible wrapper entrypoints in ``run.py``.
"""

from __future__ import annotations

from typing import Callable

from codepilot.commands.run_failure_triage_apply import (
    _apply_replan_decision,  # noqa: F401 (re-export)
    _apply_retry_with_hint_decision,  # noqa: F401 (re-export)
    _fallback_review_failure_result,  # noqa: F401 (re-export)
    _override_review_failure_discard_decision,  # noqa: F401 (re-export)
    apply_review_failure_triage,  # noqa: F401 (re-export)
)
from codepilot.commands.run_failure_triage_decisions import (
    _TriageEvidence,
    _decide_triage_action,
    _collect_review_failure_evidence as _collect_review_failure_evidence_impl,
    _collect_triage_evidence as _collect_triage_evidence_impl,
    _map_review_failure_decision,
    _map_triage_decision,
)
from codepilot.commands.run_failure_triage_prompts import (
    _DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
    _DETERMINISTIC_FAILURE_TRIAGE_SCHEMA,
    _REVIEW_FAILURE_TRIAGE_SCHEMA,
    _build_deterministic_failure_triage_prompt as _build_deterministic_failure_triage_prompt_impl,
    _build_review_failure_triage_prompt as _build_review_failure_triage_prompt_impl,
)


def _build_deterministic_failure_triage_prompt(
    task: dict,
    *,
    error_message: str,
    candidates: list[dict],
    candidate_limit: int = _DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
) -> str:
    return _build_deterministic_failure_triage_prompt_impl(
        task,
        error_message=error_message,
        candidates=candidates,
        candidate_limit=candidate_limit,
    )


def _build_review_failure_triage_prompt(
    task: dict,
    *,
    error_message: str,
    review_output: str,
    builder_output: str,
    candidates: list[dict],
    candidate_limit: int = _DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
    reviewer_verdict=None,
) -> str:
    return _build_review_failure_triage_prompt_impl(
        task,
        error_message=error_message,
        review_output=review_output,
        builder_output=builder_output,
        candidates=candidates,
        candidate_limit=candidate_limit,
        reviewer_verdict=reviewer_verdict,
    )


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
    return _collect_triage_evidence_impl(
        task,
        error_message=error_message,
        db_module=db_module,
        resolve_project_config_reference_fn=resolve_project_config_reference_fn,
        load_project_config_fn=load_project_config_fn,
        resolve_planner_fn=resolve_planner_fn,
        candidate_limit=candidate_limit,
        prompt_builder=_build_deterministic_failure_triage_prompt,
    )


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
    return _collect_review_failure_evidence_impl(
        task,
        error_message=error_message,
        review_output=review_output,
        builder_output=builder_output,
        db_module=db_module,
        resolve_project_config_reference_fn=resolve_project_config_reference_fn,
        load_project_config_fn=load_project_config_fn,
        resolve_planner_fn=resolve_planner_fn,
        candidate_limit=candidate_limit,
        prompt_builder=_build_review_failure_triage_prompt,
    )


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

    triage_decision = _decide_triage_action(
        evidence,
        schema=_DETERMINISTIC_FAILURE_TRIAGE_SCHEMA,
        call_structured_fn=call_structured_fn,
        gateway_request_cls=gateway_request_cls,
    )
    if not triage_decision:
        return None

    payload, source = triage_decision
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

    triage_decision = _decide_triage_action(
        evidence,
        schema=_REVIEW_FAILURE_TRIAGE_SCHEMA,
        call_structured_fn=call_structured_fn,
        gateway_request_cls=gateway_request_cls,
    )
    if not triage_decision:
        return None

    payload, source = triage_decision
    return _map_review_failure_decision(
        task,
        error_message=error_message,
        payload=payload,
        source=source,
        visible_candidate_ids=evidence.visible_candidate_ids,
        db_module=db_module,
    )
