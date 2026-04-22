"""Deterministic failure triage helpers for ``codepilot run``.

Split out from ``run.py`` to keep queue orchestration code focused while
preserving backwards-compatible wrapper entrypoints in ``run.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


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


def _append_task_content(base: str, note: str) -> str:
    base = (base or "").rstrip()
    note = (note or "").strip()
    if not base:
        return note
    if not note:
        return base
    return f"{base}\n\n{note}"


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
    call_structured_fn,
    gateway_request_cls,
) -> tuple[dict, str] | None:
    try:
        response = call_structured_fn(
            gateway_request_cls(
                prompt=evidence.prompt,
                schema=_DETERMINISTIC_FAILURE_TRIAGE_SCHEMA,
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
