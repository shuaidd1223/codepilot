"""Environment bridge for launching external coding agents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codepilot.ai_support.cli_families import get_family
from codepilot.ai_support.family_runtime import FamilyRuntimeUnavailable, build_env_for_family
from codepilot.ai_support.opencode_runtime import (
    OpenCodeBackendUnavailable,
    select_opencode_backend,
)
from codepilot.ai_support.providers import API_PROVIDERS
from codepilot.core.config import AgentsConfig, ConfigError, ProviderAPIConfig
from codepilot.opencode.config import OpenCodeConfig


OPENAI_PROVIDER_KEYS = {"openai", "openai-gpt4", "openai-gpt4o", "openai-gpt35"}
ANTHROPIC_PROVIDER_KEYS = {"claude-opus", "claude-sonnet", "claude-haiku"}
OPENCODE_PERMISSION_VALUES = {"ask", "allow", "deny"}
OPENCODE_PERMISSION_MODES = {
    "ask": "ask",
    "manual": "ask",
    "manual_confirm": "ask",
    "confirm": "ask",
    "full": "full_access",
    "full_access": "full_access",
    "full-access": "full_access",
    "allow": "full_access",
    "allow_all": "full_access",
    "custom": "custom",
}


@dataclass(frozen=True)
class OpenCodeProviderSpec:
    """How one CodePilot API provider is exposed to OpenCode."""

    provider_id: str
    display_name: str
    env_var: str = ""
    npm: str = ""
    default_base_url: str = ""
    deepseek_options: bool = False


def build_agent_launch_env(agent: str, cfg: AgentsConfig | None) -> dict[str, str]:
    """Return API env overrides for a CLI family launch.

    Interactive CLIs can still use their own native login flow when CodePilot
    has no matching provider key, so missing config is not a launch blocker.
    """
    if cfg is None:
        return {}
    family = get_family(agent)
    if family is not None and family.name == "opencode":
        return build_opencode_launch_env(cfg)
    try:
        return build_env_for_family(agent, cfg)
    except (FamilyRuntimeUnavailable, OpenCodeBackendUnavailable):
        return {}


def clean_agent_env(env: dict[str, str], cfg: AgentsConfig | None) -> None:
    """从 agent 启动环境中移除未配置的 API key 环境变量。

    当宿主机系统环境（如 OPENAI_API_KEY）存在但项目没有对应的已配置
    provider 时，这些 key 不应该泄漏到 agent 子进程中。
    """
    if cfg is None:
        return
    configured_vars: set[str] = set()
    for _provider_name, provider_cfg in _iter_enabled_provider_configs(cfg):
        spec = _provider_spec(_provider_name, provider_cfg)
        if spec is not None and spec.env_var:
            configured_vars.add(spec.env_var)
    for key in list(env.keys()):
        if key not in configured_vars and key.endswith("_API_KEY"):
            del env[key]


def build_opencode_launch_env(cfg: AgentsConfig | None) -> dict[str, str]:
    """Return API key env vars referenced by the generated OpenCode config."""
    if cfg is None:
        return {}
    env: dict[str, str] = {}
    for provider_name, provider_cfg in _iter_enabled_provider_configs(cfg):
        spec = _provider_spec(provider_name, provider_cfg)
        if spec is None or not spec.env_var:
            continue
        api_key = (provider_cfg.api_key or "").strip()
        if api_key:
            env[spec.env_var] = api_key
    return env


def build_opencode_config_from_agents_config(
    cfg: AgentsConfig | None,
    *,
    preferred_model: dict[str, str] | None = None,
) -> OpenCodeConfig:
    """Build CodePilot's OpenCode profile additions from provider config."""
    result = OpenCodeConfig()
    if cfg is None:
        return result

    if getattr(cfg, "automation", None):
        result.agent_language = str(getattr(cfg.automation, "agent_language", "en") or "en")

    _apply_project_permission_config(result, cfg)

    selected_provider = _select_default_provider_name(cfg)
    built_any = False
    for provider_name, provider_cfg in _iter_enabled_provider_configs(cfg):
        spec = _provider_spec(provider_name, provider_cfg)
        if spec is None:
            continue
        main_model = _main_model(provider_name, provider_cfg)
        small_model = _small_model(provider_cfg)
        provider_payload = _provider_payload(spec, provider_cfg, main_model, small_model)
        if provider_payload:
            result.providers[spec.provider_id] = provider_payload
            built_any = True
        if selected_provider == provider_name and main_model:
            result.model = _qualified_model(spec.provider_id, main_model)
            result.agent.model = result.model
            if small_model:
                result.small_model = _qualified_model(spec.provider_id, small_model)

    preferred = _qualified_preferred_model(preferred_model)
    if preferred:
        result.model = preferred
        result.agent.model = preferred

    if not result.model:
        for provider_name, provider_cfg in _iter_enabled_provider_configs(cfg):
            spec = _provider_spec(provider_name, provider_cfg)
            main_model = _main_model(provider_name, provider_cfg)
            if spec is not None and main_model:
                result.model = _qualified_model(spec.provider_id, main_model)
                result.agent.model = result.model
                small_model = _small_model(provider_cfg)
                if small_model:
                    result.small_model = _qualified_model(spec.provider_id, small_model)
                break

    if not built_any:
        return result
    return result


def _apply_project_permission_config(result: OpenCodeConfig, cfg: AgentsConfig) -> None:
    raw = getattr(cfg, "opencode_permission", None) or {}
    if not raw:
        return
    if not isinstance(raw, Mapping):
        raise ConfigError("[opencode.permission] 必须是 TOML table。")

    raw_mode = str(raw.get("mode") or "").strip().lower().replace(" ", "_")
    if raw_mode:
        mode = OPENCODE_PERMISSION_MODES.get(raw_mode)
        if mode is None:
            raise ConfigError(
                "[opencode.permission].mode 只支持 ask、full_access 或 custom。"
            )
        result.permission_mode = mode

    rules: dict[str, str] = {}
    nested_rules = raw.get("rules")
    if nested_rules is not None:
        if not isinstance(nested_rules, Mapping):
            raise ConfigError("[opencode.permission.rules] 必须是 TOML table。")
        rules.update(_normalize_permission_rules(nested_rules, "[opencode.permission.rules]"))

    inline_rules = {
        key: value
        for key, value in raw.items()
        if str(key) not in {"mode", "rules"}
    }
    if inline_rules:
        rules.update(_normalize_permission_rules(inline_rules, "[opencode.permission]"))

    if rules:
        result.permissions = rules
        if not raw_mode:
            result.permission_mode = "custom"


def _normalize_permission_rules(raw: Mapping[object, object], path: str) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip()
        if not key:
            raise ConfigError(f"{path} 包含空权限键。")
        rules[key] = _normalize_permission_value(raw_value, f"{path}.{key}")
    return rules


def _normalize_permission_value(raw_value: object, path: str) -> str | dict[str, Any]:
    if isinstance(raw_value, Mapping):
        return _normalize_permission_rules(raw_value, path)
    value = str(raw_value or "").strip().lower()
    if value not in OPENCODE_PERMISSION_VALUES:
        raise ConfigError(f"{path} 只支持 ask、allow、deny 或嵌套规则表。")
    return value


def _qualified_preferred_model(preferred_model: dict[str, str] | None) -> str:
    if not isinstance(preferred_model, dict):
        return ""
    provider_id = str(preferred_model.get("provider_id") or preferred_model.get("providerID") or "").strip()
    model_id = str(preferred_model.get("model_id") or preferred_model.get("modelID") or preferred_model.get("id") or "").strip()
    if not provider_id or not model_id:
        return ""
    return _qualified_model(provider_id, model_id)


def _main_model(provider_name: str, provider_cfg: ProviderAPIConfig) -> str:
    base_provider = API_PROVIDERS.get(provider_name)
    return (
        (provider_cfg.model or "").strip()
        or (provider_cfg.complex_model or "").strip()
        or (provider_cfg.simple_model or "").strip()
        or (getattr(base_provider, "model", "") or "").strip()
    )


def _small_model(provider_cfg: ProviderAPIConfig) -> str:
    return (provider_cfg.simple_model or "").strip()


def _qualified_model(provider_id: str, model: str) -> str:
    value = str(model or "").strip()
    if not value:
        return ""
    if "/" in value:
        return value
    return f"{provider_id}/{value}"


def _provider_payload(
    spec: OpenCodeProviderSpec,
    provider_cfg: ProviderAPIConfig,
    main_model: str,
    small_model: str,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if spec.npm:
        payload["npm"] = spec.npm
    if spec.display_name:
        payload["name"] = spec.display_name
    base_url = (provider_cfg.base_url or "").strip() or spec.default_base_url
    options: dict[str, object] = {}
    if base_url:
        options["baseURL"] = base_url
    if spec.env_var:
        options["apiKey"] = f"{{env:{spec.env_var}}}"
    if options:
        payload["options"] = options

    models: dict[str, dict[str, object]] = {}
    for model in (main_model, small_model):
        model_id = _model_id(model)
        if model_id and model_id not in models:
            models[model_id] = (
                _deepseek_model_payload(provider_cfg, model_id)
                if spec.deepseek_options
                else {"name": _model_display_name(model_id)}
            )
    if models:
        payload["models"] = models
    return payload


def _select_default_provider_name(cfg: AgentsConfig) -> str:
    for provider_name, provider_cfg in _iter_enabled_provider_configs(cfg):
        if _provider_spec(provider_name, provider_cfg) is not None and _main_model(provider_name, provider_cfg):
            return provider_name
    try:
        return select_opencode_backend(cfg).source_provider
    except OpenCodeBackendUnavailable:
        for provider_name, provider_cfg in _iter_enabled_provider_configs(cfg):
            if _provider_spec(provider_name, provider_cfg) is not None:
                return provider_name
    return ""


def _iter_enabled_provider_configs(cfg: AgentsConfig):
    for provider_name, provider_cfg in (cfg.providers or {}).items():
        if not provider_cfg.enabled:
            continue
        has_key = bool((provider_cfg.api_key or "").strip())
        base_provider = API_PROVIDERS.get(provider_name)
        default_base_url = str(getattr(base_provider, "base_url", "") or "")
        configured_base_url = (provider_cfg.base_url or "").strip() or default_base_url
        local_endpoint = configured_base_url.startswith(("http://localhost", "http://127.0.0.1"))
        if has_key or local_endpoint:
            yield provider_name, provider_cfg


def _provider_spec(provider_name: str, provider_cfg: ProviderAPIConfig) -> OpenCodeProviderSpec | None:
    base_provider = API_PROVIDERS.get(provider_name)
    default_base_url = str(getattr(base_provider, "base_url", "") or "")
    display_name = str(getattr(base_provider, "name", "") or provider_name)
    env_var = _provider_env_var(provider_name, base_provider)
    if provider_name in OPENAI_PROVIDER_KEYS:
        return OpenCodeProviderSpec(
            provider_id="openai",
            display_name="OpenAI",
            env_var=env_var or "OPENAI_API_KEY",
            default_base_url=default_base_url,
        )
    if provider_name in ANTHROPIC_PROVIDER_KEYS:
        return OpenCodeProviderSpec(
            provider_id="anthropic",
            display_name="Anthropic",
            env_var=env_var or "ANTHROPIC_API_KEY",
            default_base_url=default_base_url,
        )
    if base_provider is not None and getattr(base_provider, "provider_type", "") == "openai":
        return OpenCodeProviderSpec(
            provider_id=_sanitize_provider_id(provider_name),
            display_name=display_name,
            env_var=env_var,
            npm="@ai-sdk/openai-compatible",
            default_base_url=default_base_url,
            deepseek_options=provider_name == "deepseek",
        )
    if (provider_cfg.base_url or "").strip():
        return OpenCodeProviderSpec(
            provider_id=_sanitize_provider_id(provider_name),
            display_name=display_name,
            env_var=env_var or _default_env_var(provider_name),
            npm="@ai-sdk/openai-compatible",
            default_base_url="",
        )
    return None


def _provider_env_var(provider_name: str, base_provider: object | None) -> str:
    env_vars = tuple(getattr(base_provider, "api_env_vars", ()) or ())
    if env_vars:
        return str(env_vars[0])
    if provider_name == "openai":
        return "OPENAI_API_KEY"
    return ""


def _default_env_var(provider_name: str) -> str:
    return f"{_sanitize_provider_id(provider_name).upper().replace('-', '_')}_API_KEY"


def _sanitize_provider_id(provider_name: str) -> str:
    return str(provider_name or "").strip().lower().replace("_", "-")


def _deepseek_model_payload(provider_cfg: ProviderAPIConfig, model_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": _deepseek_model_display_name(model_id),
        "limit": {
            "context": 1048576,
            "output": 262144,
        },
    }
    if model_id.endswith("-pro") or model_id == "deepseek-reasoner":
        payload["options"] = {
            "reasoningEffort": _deepseek_reasoning_effort(provider_cfg),
            "thinking": {"type": "enabled"},
        }
    return payload


def _deepseek_reasoning_effort(provider_cfg: ProviderAPIConfig) -> str:
    value = (provider_cfg.reasoning_effort or "").strip().lower()
    if value in {"low", "medium", "high", "max", "minimal"}:
        return value
    return "max"


def _deepseek_model_display_name(model_id: str) -> str:
    return str(model_id or "").replace("deepseek", "DeepSeek").replace("-v", "-V").replace("-pro", "-Pro").replace(
        "-flash", "-Flash"
    )


def _model_id(model: str) -> str:
    value = str(model or "").strip()
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


def _model_display_name(model: str) -> str:
    return str(model or "").replace("-", " ").upper().replace(" ", "-")
