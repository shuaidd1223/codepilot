"""Provider dataclasses, built-in registries, and config resolution."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from codepilot.ai_support.provider_adapters import build_api_client
from codepilot.core.config import load_project_config


@dataclass
class CLIProvider:
    """Command-line AI provider configuration."""

    name: str
    cmd: str
    args_template: list[str] = field(default_factory=list)
    env_prepend: dict = field(default_factory=dict)
    timeout: int = 180

    def find_executable(self) -> Optional[Path]:
        """Find the configured executable."""
        if Path(self.cmd).exists():
            return Path(self.cmd)
        found = shutil.which(self.cmd)
        return Path(found) if found else None


@dataclass
class APIProvider:
    """API AI provider configuration."""

    name: str
    provider_type: str
    model: str
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4096
    temperature: float = 0.7
    api_env_vars: tuple[str, ...] = ()
    usage_key: str = ""
    auto_model_selection: bool = False
    simple_model: str = ""
    complex_model: str = ""
    thinking: str = ""
    reasoning_effort: str = ""
    balance_endpoint: str = ""

    def requires_api_key(self) -> bool:
        """Whether this provider needs an API key."""
        if self.base_url.startswith(("http://localhost", "http://127.0.0.1")):
            return False
        return True

    def resolve_api_key(self) -> str:
        """Resolve API key from explicit config or supported environment variables."""
        if self.api_key:
            return self.api_key
        for env_var in self.api_env_vars:
            value = os.environ.get(env_var, "").strip()
            if value:
                return value
        if not self.requires_api_key():
            return "local-provider"
        return ""

    def build_client(self):
        """Build the API client."""
        return build_api_client(self)


CLI_PROVIDERS: dict[str, CLIProvider] = {
    "claude": CLIProvider(
        name="Claude Code",
        cmd="claude",
        args_template=["-p", "{prompt}", "--output-format", "text", "--dangerously-skip-permissions"],
        timeout=180,
    ),
    "claude-node": CLIProvider(
        name="Claude Code (Node)",
        cmd="node",
        args_template=[
            "{node_modules}/@anthropic-ai/claude-code/cli.js",
            "-p",
            "{prompt}",
            "--output-format",
            "text",
            "--dangerously-skip-permissions",
        ],
        timeout=180,
    ),
    "codex": CLIProvider(
        name="OpenAI Codex",
        cmd="codex",
        args_template=[
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "{prompt}",
        ],
        timeout=180,
    ),
    "opencode": CLIProvider(
        name="OpenCode",
        cmd="opencode",
        args_template=["run", "{prompt}"],
        timeout=180,
    ),
    "gemini": CLIProvider(
        name="Google Gemini CLI",
        cmd="gemini",
        args_template=["-p", "{prompt}"],
        timeout=180,
    ),
    "cloud": CLIProvider(
        name="Tencent Cloud CLI",
        cmd="cloud",
        args_template=["-p", "{prompt}"],
        timeout=180,
    ),
}

API_PROVIDERS: dict[str, APIProvider] = {
    "openai-gpt4": APIProvider(
        name="OpenAI GPT-4",
        provider_type="openai",
        model="gpt-4-turbo-preview",
        max_tokens=4096,
        api_env_vars=("OPENAI_API_KEY",),
    ),
    "openai-gpt4o": APIProvider(
        name="OpenAI GPT-4o",
        provider_type="openai",
        model="gpt-4o",
        max_tokens=4096,
        api_env_vars=("OPENAI_API_KEY",),
    ),
    "openai-gpt35": APIProvider(
        name="OpenAI GPT-3.5 Turbo",
        provider_type="openai",
        model="gpt-3.5-turbo",
        max_tokens=2048,
        api_env_vars=("OPENAI_API_KEY",),
    ),
    "claude-opus": APIProvider(
        name="Claude 3 Opus",
        provider_type="anthropic",
        model="claude-opus-4-20241120",
        max_tokens=4096,
        api_env_vars=("ANTHROPIC_API_KEY",),
    ),
    "claude-sonnet": APIProvider(
        name="Claude 3.5 Sonnet",
        provider_type="anthropic",
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        api_env_vars=("ANTHROPIC_API_KEY",),
    ),
    "claude-haiku": APIProvider(
        name="Claude 3 Haiku",
        provider_type="anthropic",
        model="claude-3-5-haiku-20240307",
        max_tokens=2048,
        api_env_vars=("ANTHROPIC_API_KEY",),
    ),
    "hunyuan": APIProvider(
        name="腾讯云混元",
        provider_type="openai",
        model="hunyuan",
        base_url="https://hunyuan.cloud.tencent.com",
        max_tokens=4096,
        api_env_vars=("HUNYUAN_API_KEY",),
    ),
    "zhipu-glm4": APIProvider(
        name="智谱 GLM-4",
        provider_type="openai",
        model="glm-4",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        max_tokens=4096,
        api_env_vars=("ZHIPU_API_KEY",),
    ),
    "wenxin": APIProvider(
        name="百度文心一言",
        provider_type="openai",
        model="ernie-4.0-8k-latest",
        base_url="https://qianfan.baidubce.com/v2",
        max_tokens=4096,
        api_env_vars=("ERNIE_API_KEY",),
    ),
    "qwen": APIProvider(
        name="阿里通义千问",
        provider_type="openai",
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_tokens=4096,
        api_env_vars=("DASHSCOPE_API_KEY",),
    ),
    "deepseek": APIProvider(
        name="DeepSeek",
        provider_type="openai",
        model="",
        base_url="https://api.deepseek.com",
        max_tokens=8192,
        api_env_vars=("DEEPSEEK_API_KEY",),
        usage_key="deepseek",
        auto_model_selection=True,
        simple_model="deepseek-v4-flash",
        complex_model="deepseek-v4-pro",
        thinking="auto",
        reasoning_effort="auto",
        balance_endpoint="/user/balance",
    ),
    "ollama": APIProvider(
        name="Ollama (本地)",
        provider_type="openai",
        model="llama3",
        base_url="http://localhost:11434/v1",
        max_tokens=4096,
        api_env_vars=(),
    ),
    "groq": APIProvider(
        name="Groq",
        provider_type="openai",
        model="llama-3.1-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        max_tokens=4096,
        api_env_vars=("GROQ_API_KEY",),
    ),
}


def _build_cli_command_env_vars() -> dict[str, str]:
    from codepilot.ai_support.cli_families import CLI_FAMILIES

    return {family.name: family.env_var for family in CLI_FAMILIES.values()}


CLI_COMMAND_ENV_VARS: dict[str, str] = _build_cli_command_env_vars()


def _resolve_api_provider_from_registry(
    provider_key: str,
    registry: dict[str, APIProvider],
    project_path: str | Path | dict | None = None,
    *,
    config_file: str | Path | None = None,
) -> APIProvider:
    """Return an API provider with AGENTS.toml overrides applied."""
    if provider_key not in registry:
        raise KeyError(provider_key)

    provider = replace(registry[provider_key])
    cfg = load_project_config(project_path, config_file=config_file)
    provider_cfg = cfg.providers.get(provider_key) if cfg else None
    if not provider_cfg:
        return provider

    if provider_cfg.api_key:
        provider.api_key = provider_cfg.api_key.strip()
    if provider_cfg.model:
        provider.model = provider_cfg.model.strip()
        provider.auto_model_selection = False
    if provider_cfg.base_url:
        provider.base_url = provider_cfg.base_url.strip()
    provider.max_tokens = int(provider_cfg.max_tokens or provider.max_tokens)
    provider.temperature = float(provider_cfg.temperature)
    if provider_cfg.auto_model_selection is not None:
        provider.auto_model_selection = bool(provider_cfg.auto_model_selection)
    if provider_cfg.simple_model:
        provider.simple_model = provider_cfg.simple_model.strip()
    if provider_cfg.complex_model:
        provider.complex_model = provider_cfg.complex_model.strip()
    if provider_cfg.thinking:
        provider.thinking = provider_cfg.thinking.strip().lower()
    if provider_cfg.reasoning_effort:
        provider.reasoning_effort = provider_cfg.reasoning_effort.strip().lower()
    return provider


def resolve_api_provider(
    provider_key: str,
    project_path: str | Path | dict | None = None,
    *,
    config_file: str | Path | None = None,
) -> APIProvider:
    """Return an API provider with AGENTS.toml overrides applied."""
    return _resolve_api_provider_from_registry(
        provider_key,
        API_PROVIDERS,
        project_path,
        config_file=config_file,
    )
