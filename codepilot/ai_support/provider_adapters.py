"""API provider adapter registry.

This module keeps provider-type specific client construction out of the
provider dataclass so new API-compatible backends can be added by registering
an adapter instead of editing conditional branches in ``providers.py``.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Callable

OPENAI_AVAILABLE = importlib.util.find_spec("openai") is not None
ANTHROPIC_AVAILABLE = importlib.util.find_spec("anthropic") is not None

APIClientAdapter = Callable[[Any], tuple[Any, str]]


def _api_key_env_names(provider: Any) -> str:
    return " / ".join(getattr(provider, "api_env_vars", ()) or ()) or "对应的 API Key 环境变量"


def _require_api_key(provider: Any, api_key: str) -> None:
    if provider.requires_api_key() and not api_key:
        raise RuntimeError(
            f"当前无法使用 {provider.name}，因为还没有配置 API Key。"
            f"请先设置 {_api_key_env_names(provider)}。"
        )


def build_openai_client(provider: Any) -> tuple[Any, str]:
    if not OPENAI_AVAILABLE:
        raise RuntimeError(
            f"当前无法使用 {provider.name}，因为本机没有安装 openai 依赖。"
            "请先执行: pip install openai"
        )
    api_key = provider.resolve_api_key()
    _require_api_key(provider, api_key)

    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=provider.base_url or None,
    )
    return client, "chat.completions"


def build_anthropic_client(provider: Any) -> tuple[Any, str]:
    if not ANTHROPIC_AVAILABLE:
        raise RuntimeError(
            f"当前无法使用 {provider.name}，因为本机没有安装 anthropic 依赖。"
            "请先执行: pip install anthropic"
        )
    api_key = provider.resolve_api_key()
    _require_api_key(provider, api_key)

    from anthropic import Anthropic

    client = Anthropic(
        api_key=api_key,
        base_url=provider.base_url or None,
    )
    return client, "messages"


API_CLIENT_ADAPTERS: dict[str, APIClientAdapter] = {
    "openai": build_openai_client,
    "anthropic": build_anthropic_client,
}


def build_api_client(provider: Any) -> tuple[Any, str]:
    provider_type = str(getattr(provider, "provider_type", "") or "")
    adapter = API_CLIENT_ADAPTERS.get(provider_type)
    if adapter is None:
        raise ValueError(f"不支持的 provider_type: {provider_type}")
    return adapter(provider)
