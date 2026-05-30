"""API provider request profile decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class APIRequestProfile:
    model: str
    difficulty: str
    thinking: str = ""
    reasoning_effort: str = ""


_COMPLEX_TASK_KEYWORDS = (
    "架构",
    "重构",
    "迁移",
    "兼容",
    "回归",
    "并发",
    "数据库",
    "调度",
    "发布",
    "安全",
    "权限",
    "多模块",
    "高风险",
    "性能",
    "分布式",
    "integration",
    "migration",
    "architecture",
    "refactor",
    "compatibility",
    "concurrency",
    "database",
    "security",
    "regression",
)

_SIMPLE_TASK_KEYWORDS = (
    "总结",
    "翻译",
    "分类",
    "一句话",
    "解释",
    "摘要",
    "summarize",
    "translate",
    "categorize",
)

_DEEPSEEK_COMPLEX_DIFFICULTIES = {"hard", "xhard"}


def estimate_prompt_difficulty(prompt: str, system_prompt: Optional[str] = None) -> str:
    """Small deterministic heuristic for cost/quality-sensitive API calls."""
    text = f"{system_prompt or ''}\n{prompt or ''}".lower()
    char_count = len(text)
    score = 0
    if char_count > 12000:
        score += 5
    elif char_count > 6000:
        score += 4
    elif char_count > 2500:
        score += 2
    elif char_count > 900:
        score += 1

    keyword_hits = sum(1 for word in _COMPLEX_TASK_KEYWORDS if word in text)
    score += min(5, keyword_hits)
    if keyword_hits >= 5:
        score += 2
    if any(word in text for word in _SIMPLE_TASK_KEYWORDS) and char_count < 1200:
        score -= 2

    if score >= 7:
        return "xhard"
    if score >= 4:
        return "hard"
    if score >= 2:
        return "medium"
    return "simple"


def provider_usage_key(provider: Any) -> str:
    explicit = str(getattr(provider, "usage_key", "") or "").strip()
    if explicit:
        return explicit
    name = str(getattr(provider, "name", "") or "").strip().lower()
    return name.replace(" ", "-") or "unknown"


def is_deepseek_provider(provider: Any) -> bool:
    base_url = str(getattr(provider, "base_url", "") or "").lower()
    return provider_usage_key(provider) == "deepseek" or "api.deepseek.com" in base_url


def _select_deepseek_model(provider: Any, difficulty: str, base_model: str) -> str:
    simple_model = str(getattr(provider, "simple_model", "") or "").strip()
    complex_model = str(getattr(provider, "complex_model", "") or "").strip()
    if bool(getattr(provider, "auto_model_selection", False)):
        if not simple_model or not complex_model:
            raise RuntimeError(
                f"{getattr(provider, 'name', 'provider')} 已开启自动模型切换，"
                "但 simple_model / complex_model 未配置完整。"
            )
        return complex_model if difficulty in _DEEPSEEK_COMPLEX_DIFFICULTIES else simple_model
    return base_model or simple_model or complex_model


def _resolve_deepseek_thinking(provider: Any, difficulty: str) -> str:
    configured = str(getattr(provider, "thinking", "") or "auto").strip().lower()
    if configured in {"enabled", "disabled"}:
        return configured
    if configured == "auto":
        return "enabled" if difficulty in _DEEPSEEK_COMPLEX_DIFFICULTIES else "disabled"
    return ""


def _resolve_deepseek_reasoning_effort(provider: Any, difficulty: str, thinking: str) -> str:
    if thinking != "enabled":
        return ""
    configured = str(getattr(provider, "reasoning_effort", "") or "auto").strip().lower()
    if configured in {"high", "max"}:
        return configured
    return "max" if difficulty == "xhard" else "high"


def build_api_request_profile(
    provider: Any,
    prompt: str,
    system_prompt: Optional[str] = None,
) -> APIRequestProfile:
    base_model = str(getattr(provider, "model", "") or "").strip()
    difficulty = estimate_prompt_difficulty(prompt, system_prompt)
    model = base_model
    thinking = ""
    reasoning_effort = ""

    if is_deepseek_provider(provider):
        model = _select_deepseek_model(provider, difficulty, base_model)
        thinking = _resolve_deepseek_thinking(provider, difficulty)
        reasoning_effort = _resolve_deepseek_reasoning_effort(provider, difficulty, thinking)

    return APIRequestProfile(
        model=model,
        difficulty=difficulty,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )
