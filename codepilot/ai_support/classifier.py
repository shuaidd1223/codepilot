"""Intent classification and quick-answer helpers.

Split out from ai.py. Re-exported via `codepilot.ai_support.service`. The API /
CLI routing itself lives in :mod:`codepilot.gateway`; this module keeps the
public compatibility surface while the implementation is split by concern.
"""

from __future__ import annotations

from typing import Optional

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
from codepilot.ai_support.providers import _collect_project_context
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


def _resolve_gateway_options(
    *,
    gateway_options: Optional[GatewayCallOptions],
    classifier_provider: str,
    classifier_model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    project_path: str,
    config_ref: str,
    planner: str,
    timeout: int,
) -> GatewayCallOptions:
    """Merge explicit kwargs with an optional shared gateway context object."""
    shared = gateway_options
    effective_timeout = timeout
    if shared is not None and shared.timeout:
        effective_timeout = int(shared.timeout)
    return GatewayCallOptions(
        classifier_provider=(shared.classifier_provider if shared else "") or classifier_provider,
        classifier_model=(shared.classifier_model if shared else "") or classifier_model,
        api_key=shared.api_key if shared and shared.api_key is not None else api_key,
        base_url=shared.base_url if shared and shared.base_url is not None else base_url,
        project_path=(shared.project_path if shared else "") or project_path,
        config_ref=(shared.config_ref if shared else "") or config_ref,
        planner=planner,
        timeout=effective_timeout,
    )


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
    text = text.strip()
    if not text:
        return {"intent": "requirement", "reason": "空输入", "source": "default"}

    guess = _heuristic_intent(text)
    if guess:
        return {"intent": guess, "reason": "启发式规则命中", "source": "heuristic"}

    valid_intents = {"question", "task", "requirement", "command"}

    from codepilot.gateway.service import call_structured_prompt

    shared_options = _resolve_gateway_options(
        gateway_options=gateway_options,
        classifier_provider=classifier_provider,
        classifier_model=classifier_model,
        api_key=api_key,
        base_url=base_url,
        project_path=project_path,
        config_ref=config_ref,
        planner="claude",
        timeout=timeout,
    )
    from codepilot.core import progress_bus

    with progress_bus.llm_context(stage="planner", label="意图分类"):
        response = call_structured_prompt(
            prompt=INTENT_PROMPT.format(text=text),
            schema=INTENT_SCHEMA,
            # Classification is latency-sensitive; keep claude as CLI fallback family.
            options=shared_options,
        )

    if response.ok and response.payload:
        intent = response.payload.get("intent")
        if intent in valid_intents:
            if intent == "command" and not _looks_like_codepilot_command(text):
                return {
                    "intent": "requirement",
                    "reason": "命令防误判兜底：输入不符合 codepilot 命令形态",
                    "source": "guardrail",
                }
            return {
                "intent": intent,
                "reason": response.payload.get("reason", ""),
                "source": response.source,
            }

    return {
        "intent": "requirement",
        "reason": f"分类失败，默认当作需求处理（{response.error or '未知原因'}）",
        "source": "default",
    }


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
    local_answer = _local_tool_question_answer(question)
    if local_answer:
        return local_answer

    context = _collect_project_context(project_path, query_text=question)
    history_block = ""
    if history:
        lines = []
        for turn in history[-10:]:  # 最多保留最近 10 轮
            lines.append(f"用户: {turn['user']}")
            if turn.get("assistant"):
                lines.append(f"助手: {turn['assistant'][:300]}")
        history_block = "\n## 对话历史\n" + "\n".join(lines) + "\n"

    from codepilot.gateway.service import call_text_prompt

    shared_options = _resolve_gateway_options(
        gateway_options=gateway_options,
        classifier_provider=provider_key,
        classifier_model=model_override,
        api_key=api_key,
        base_url=base_url,
        project_path=project_path,
        config_ref=config_ref,
        planner="claude",
        timeout=120,
    )
    has_remote_capability = _provider_has_remote_answer_capability(provider_key, api_key, base_url)
    has_local_agent = _has_local_question_answer_agent()
    runtime_bundle: dict = {}
    try:
        runtime_bundle = _question_runtime_bundle(
            question,
            options=shared_options,
            project_path=shared_options.project_path or project_path,
            allow_model_planner=has_remote_capability,
        )
    except Exception:
        runtime_bundle = {}

    local_runtime_answer = _render_runtime_lookup_answer(question, runtime_bundle)
    general_local_answer = _local_general_question_answer(
        question,
        project_path=shared_options.project_path or project_path,
    )
    if local_runtime_answer and not has_remote_capability and not has_local_agent:
        return local_runtime_answer
    if general_local_answer and not has_remote_capability and not has_local_agent:
        return general_local_answer
    from codepilot.core import progress_bus

    runtime_data_block = _question_runtime_bundle_json(runtime_bundle)
    runtime_data_section = (
        f"\n## 本地取数结果\n{runtime_data_block}\n"
        if runtime_data_block
        else ""
    )
    prompt_header = _question_prompt_header(shared_options.project_path or project_path)
    with progress_bus.llm_context(stage="system", label="问题回答"):
        response = call_text_prompt(
            prompt=(
                f"{prompt_header}"
                "请基于下面的项目上下文、必要时的本地取数结果和对话历史，用简洁中文直接回答用户问题。"
                "如果已经给了本地取数结果，优先使用这些确定数据，不要忽略。"
                "不要只回复“我不确定”。\n\n"
                f"## 项目上下文\n{context}\n"
                f"{history_block}"
                f"{runtime_data_section}"
                f"## 用户问题\n{question}"
            ),
            options=shared_options,
        )
    if response.ok and response.text:
        return response.text
    if local_runtime_answer:
        return local_runtime_answer
    return general_local_answer if general_local_answer else ""
