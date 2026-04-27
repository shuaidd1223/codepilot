"""Evidence collection and decision mapping for run failure triage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from codepilot.commands.reviewer_output import parse_reviewer_output
from codepilot.commands.run_failure_triage_prompts import (
    _append_task_content,
    _build_deterministic_failure_triage_prompt,
    _build_review_failure_triage_prompt,
    _format_replan_task_content,
    _format_retry_hint_note,
    _format_triage_merge_note,
    _iter_triage_candidates,
    _trim_triage_text,
)


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
    prompt_builder=_build_deterministic_failure_triage_prompt,
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
    prompt = prompt_builder(
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
    prompt_builder=_build_review_failure_triage_prompt,
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
    try:
        reviewer_verdict = parse_reviewer_output(review_output or "")
    except Exception:
        reviewer_verdict = None
    prompt = prompt_builder(
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
