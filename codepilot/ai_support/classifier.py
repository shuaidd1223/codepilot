"""Intent classification and quick-answer helpers.

Split out from ai.py. Re-exported via `codepilot.ai_support.service`. The API /
CLI routing itself lives in :mod:`codepilot.gateway`; this module keeps the
public compatibility surface while the implementation is split by concern.
"""

from __future__ import annotations

from typing import Optional

from codepilot.ai_support.gateway_options import _resolve_gateway_options
from codepilot.ai_support.intent_classifier import classify_intent_with_rules
from codepilot.ai_support.intent_rules import (
    INTENT_PROMPT,
    INTENT_SCHEMA,
    _heuristic_intent,
    _is_tool_manifest_question,
    _local_tool_question_answer,
    _looks_like_codepilot_command,
    _looks_like_information_request,
    _normalize_question_key,
    _question_likely_needs_runtime_data,
    _question_mentions_project_status,
    _question_mentions_projects,
    _question_mentions_task_totals,
    _question_mentions_tool_usage,
)
from codepilot.ai_support.question_answering import answer_question_with_runtime
from codepilot.ai_support.question_runtime import (
    QUESTION_LOOKUP_PLAN_SCHEMA,
    _add_question_lookup,
    _empty_runtime_lookup_result,
    _execute_question_runtime_lookups,
    _execute_runtime_lookup_request,
    _has_local_question_answer_agent,
    _heuristic_question_runtime_plan,
    _local_general_question_answer,
    _local_project_status_answer,
    _local_projects_answer,
    _local_task_totals_answer,
    _local_tool_usage_answer,
    _lookup_entry_items,
    _plan_question_runtime_lookups,
    _project_identity,
    _project_runtime_snapshot,
    _provider_has_remote_answer_capability,
    _question_lookup_plan_prompt,
    _question_prompt_header,
    _question_runtime_bundle,
    _question_runtime_bundle_json,
    _question_runtime_data_block,
    _question_runtime_lookup_requests,
    _render_project_list_fallback,
    _render_runtime_lookup_answer,
    _render_runtime_lookup_entry,
    _render_service_status_lookup,
    _render_status_task_lookup,
    _render_task_list_lookup,
    _render_task_refs,
    _render_task_stats_lookup,
    _resolve_question_project,
    _resolve_question_runtime_plan,
    _runtime_lookup_limit,
    _runtime_lookup_target_projects,
    _runtime_service_snapshot,
    _service_status_lookup_items,
    _status_task_lookup_items,
    _task_list_lookup_items,
    _task_lookup_item,
    _task_lookup_items,
    _task_stats_lookup_items,
)
from codepilot.gateway.types import GatewayCallOptions


def classify_intent(
    text: str,
    project_path: str = "",
    classifier_provider: str = "",
    classifier_model: str = "",
    timeout: int = 30,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    config_ref: str = "",
    gateway_options: Optional[GatewayCallOptions] = None,
) -> dict:
    """Classify a chat input as question / task / requirement.

    Strategy:
      1. Heuristic pre-filter (free, instant).
      2. Unified AI gateway: configured API provider, then local CLI fallback.
      3. On any failure, default to 'requirement' (preserves prior behavior).
    """
    return classify_intent_with_rules(
        text,
        project_path=project_path,
        classifier_provider=classifier_provider,
        classifier_model=classifier_model,
        timeout=timeout,
        api_key=api_key,
        base_url=base_url,
        config_ref=config_ref,
        gateway_options=gateway_options,
        heuristic_intent=_heuristic_intent,
        looks_like_codepilot_command=_looks_like_codepilot_command,
    )


def answer_question_via_api(
    provider_key: str,
    question: str,
    project_path: str = "",
    model_override: str = "",
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    history: list[dict] | None = None,
    config_ref: str = "",
    gateway_options: Optional[GatewayCallOptions] = None,
) -> str:
    """Answer a user question directly without creating a task.

    Routes through :mod:`codepilot.gateway` so API / CLI fallback and
    key-resolution behaviour stay consistent with ``classify_intent``.
    """
    return answer_question_with_runtime(
        provider_key,
        question,
        project_path=project_path,
        model_override=model_override,
        api_key=api_key,
        base_url=base_url,
        history=history,
        config_ref=config_ref,
        gateway_options=gateway_options,
        has_local_question_answer_agent=_has_local_question_answer_agent,
    )
