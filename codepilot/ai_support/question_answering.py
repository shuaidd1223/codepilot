"""Direct question answering orchestration for classifier-facing chat flows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from codepilot.ai_support.gateway_options import _resolve_gateway_options
from codepilot.ai_support.intent_rules import _local_tool_question_answer
from codepilot.ai_support.providers import _collect_project_context
from codepilot.ai_support.question_runtime import (
    _local_general_question_answer,
    _provider_has_remote_answer_capability,
    _question_prompt_header,
    _question_runtime_bundle,
    _question_runtime_bundle_json,
    _render_runtime_lookup_answer,
)
from codepilot.gateway.types import GatewayCallOptions


def answer_question_with_runtime(
    provider_key: str,
    question: str,
    *,
    project_path: str = "",
    model_override: str = "",
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    history: list[dict] | None = None,
    config_ref: str = "",
    gateway_options: Optional[GatewayCallOptions] = None,
    has_local_question_answer_agent: Callable[[], bool],
) -> str:
    """Answer a user question using local facts first, then the gateway."""
    local_answer = _local_tool_question_answer(question)
    if local_answer:
        return local_answer

    context = _collect_project_context(project_path, query_text=question)
    history_block = _render_history_block(history)

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
    has_local_agent = has_local_question_answer_agent()
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


def _render_history_block(history: list[dict] | None) -> str:
    if not history:
        return ""
    lines = []
    for turn in history[-10:]:
        lines.append(f"用户: {turn['user']}")
        if turn.get("assistant"):
            lines.append(f"助手: {turn['assistant'][:300]}")
    return "\n## 对话历史\n" + "\n".join(lines) + "\n"
