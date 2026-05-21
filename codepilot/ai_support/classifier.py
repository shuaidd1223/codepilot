"""Intent classification and quick-answer helpers.

Split out from ai.py. Re-exported via `codepilot.ai_support.service`. The API /
CLI routing itself lives in :mod:`codepilot.gateway`; this module keeps the
public compatibility surface while the implementation is split by concern.
"""

from __future__ import annotations

from typing import Optional

from codepilot.ai_support.intent_classifier import classify_intent_with_rules
from codepilot.ai_support.intent_rules import (
    INTENT_SCHEMA,  # noqa: F401 (compat re-export)
    _heuristic_intent,
    _looks_like_codepilot_command,
)
from codepilot.ai_support.question_answering import answer_question_with_runtime
from codepilot.ai_support.question_runtime import _has_local_question_answer_agent
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
