"""Intent classification orchestration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from codepilot.ai_support.gateway_options import _resolve_gateway_options
from codepilot.ai_support.intent_rules import INTENT_PROMPT, INTENT_SCHEMA
from codepilot.gateway.types import GatewayCallOptions


def classify_intent_with_rules(
    text: str,
    *,
    project_path: str = "",
    classifier_provider: str = "",
    classifier_model: str = "",
    timeout: int = 30,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    config_ref: str = "",
    gateway_options: Optional[GatewayCallOptions] = None,
    heuristic_intent: Callable[[str], Optional[str]],
    looks_like_codepilot_command: Callable[[str], bool],
) -> dict:
    """Classify chat input while keeping rule dependencies injectable."""
    text = text.strip()
    if not text:
        return {"intent": "requirement", "reason": "空输入", "source": "default"}

    guess = heuristic_intent(text)
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
            options=shared_options,
        )

    if response.ok and response.payload:
        intent = response.payload.get("intent")
        if intent in valid_intents:
            if intent == "command" and not looks_like_codepilot_command(text):
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
