"""AGENTS.toml 配置发现与解析（扩展版，支持多 AI Provider）."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from codepilot.core.config_parse import (
    DEFAULT_AGENT_COMMANDS,
    DEFAULT_FALLBACK_CLI_ORDER,
    ConfigError,
    _normalize_optional_agent_name,
    normalize_agent_language,
    normalize_preflight_dirty_worktree,
)

CONFIG_FILENAME = "AGENTS.toml"
SECRETS_FILENAME = ".codepilot.secrets.toml"  # sibling file; never commit

# Preferred env var holding a secrets file path. When set, it overrides
# the default sibling-file discovery — useful for CI / containerised runs
# where the secrets file lives outside the repo.
SECRETS_PATH_ENV = "CODEPILOT_SECRETS_PATH"
GLOBAL_CONFIG_PATH_ENV = "CODEPILOT_GLOBAL_CONFIG_PATH"

__all__ = [
    "AgentsConfig",
    "AutomationConfig",
    "ClassifierConfig",
    "ConfigError",
    "DEFAULT_AGENT_COMMANDS",
    "DEFAULT_FALLBACK_CLI_ORDER",
    "DEFAULT_TEMPLATE",
    "DispatchConfig",
    "EventAgentConfig",
    "GLOBAL_CONFIG_PATH_ENV",
    "InspectConfig",
    "ProjectConfig",
    "ProviderAPIConfig",
    "SECRETS_FILENAME",
    "SECRETS_PATH_ENV",
    "ScheduledAgentConfig",
    "ShellConfig",
    "build_provider_env_vars",
    "find_config",
    "find_global_config",
    "find_project_root",
    "load_config",
    "load_project_config",
    "normalize_agent_language",
    "normalize_preflight_dirty_worktree",
    "resolve_config_path",
    "resolve_global_config_path",
    "resolve_planner",
    "resolve_project_config_inputs",
    "resolve_project_config_reference",
    "sanitize_config_for_display",
    "tomllib",
]


def resolve_global_config_path() -> Path:
    """Return the global AGENTS.toml path under the CodePilot root."""
    override = os.environ.get(GLOBAL_CONFIG_PATH_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            return (candidate / CONFIG_FILENAME).resolve()
        return candidate.resolve()

    from codepilot.core.paths import global_storage_root

    return (global_storage_root() / CONFIG_FILENAME).resolve()


def find_global_config() -> Optional[Path]:
    """Return the global AGENTS.toml path when it exists."""
    candidate = resolve_global_config_path()
    return candidate if candidate.is_file() else None


# ═══════════════════════════════════════════════════════════════════════════════
# 配置数据模型
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ProjectConfig:
    """[project] 项目配置."""
    name: str = ""
    base_branch: str = "dev"
    default_mode: str = "dual"
    worktree_base: Optional[str] = None


@dataclass
class ShellConfig:
    """Shell 配置（跨平台）."""
    preferred: str = "auto"  # 自动检测
    powershell_path: Optional[str] = None  # 自定义 PowerShell 路径
    bash_path: Optional[str] = None


@dataclass
class DispatchConfig:
    """[dispatch] 调度配置."""
    dispatch_path: str = ""
    interval_seconds: int = 600
    stale_minutes: int = 30


@dataclass
class ScheduledAgentConfig:
    """[automation.scheduled_agents.<name>] 定时智能体配置."""
    enabled: bool = True
    agent: str = ""
    prompt: str = ""
    interval: Optional[str] = None
    interval_seconds: Optional[int] = None
    schedule: Optional[str] = None
    max_cost_usd: Optional[float] = None
    max_daily_cost_usd: Optional[float] = None


@dataclass
class EventAgentConfig:
    """[automation.event_agents.<name>] 事件触发智能体配置."""
    enabled: bool = True
    trigger: str = ""
    agent: str = ""
    prompt: str = ""
    max_cost_usd: Optional[float] = None
    max_daily_cost_usd: Optional[float] = None


@dataclass
class AutomationConfig:
    """[automation] 自动规划和执行配置."""
    planner: str = "codex"
    default_agent: str = ""
    task_agent: str = "dual"
    executor: str = "builtin"
    auto_execute: bool = True
    confirm_before_execute: bool = False
    auto_commit: bool = True
    max_tasks: int = 5
    max_retries: int = 3
    per_task_branch: bool = True
    # direct: 直接在项目工作目录/base_branch 开发；
    # branch: 在项目工作目录创建任务分支，完成后合并并删除；
    # worktree: 为每个任务创建独立 git worktree，并链接常见依赖目录。
    task_workspace: str = "branch"
    # 创建任务 worktree 后复制的本地上下文文件/目录；None 表示使用工具默认值。
    worktree_context_patterns: tuple[str, ...] | None = None
    # 创建任务 worktree 后链接的本地依赖目录；None 表示使用工具默认值。
    worktree_context_link_patterns: tuple[str, ...] | None = None
    # 执行预检发现已有未提交改动时的处理策略：
    # stop: 停止执行；commit: 先提交现有改动；stash: stash 现有改动并记录。
    preflight_dirty_worktree: str = "stop"
    # 两阶段规划：先让 planner 读代码（侦察），再拆任务。关闭后变回一次性规划。
    two_stage_planning: bool = True
    # Builder-Reviewer 闭环最大轮数。reviewer 判 FAIL 时, builder 拿 reviewer
    # 反馈再做一次, 循环最多这么多轮。设为 1 等于关闭闭环（老行为）。
    max_review_rounds: int = 2
    # 子进程 (codex / claude / opencode CLI) 连续多少秒没有新输出就认为卡死并
    # kill，0 表示关闭该保护。默认关闭以避免误杀慢任务；运维 daemon 可以按需开启。
    agent_silence_timeout_seconds: int = 0
    # workflow next --auto 的保守自动推进策略。默认只执行现有低风险动作：
    # 忽略已被负反馈降权的 report-only 巡检项，以及为 inspect context 生成可审查 plan。
    # 不自动创建 backlog 任务、不自动导入 plan 任务。
    workflow_auto_create_inspect_tasks: bool = False
    workflow_auto_import_plan_tasks: bool = False
    workflow_auto_max_steps: int = 1
    workflow_auto_failure_threshold: int = 1
    # 文本模式 CLI 兜底顺序：缺失或不可用时按此列表向后退。
    # 默认 ["claude", "codex", "opencode"]，opencode 作为最终兜底（用已配 API key）。
    fallback_cli_order: list[str] = field(default_factory=lambda: list(DEFAULT_FALLBACK_CLI_ORDER))
    # 智能体 prompt / 任务内容 / 输出语言偏好。仅影响 agent-facing 内容。
    agent_language: str = "en"
    scheduled_agents: dict[str, ScheduledAgentConfig] = field(default_factory=dict)
    event_agents: dict[str, EventAgentConfig] = field(default_factory=dict)


@dataclass
class InspectConfig:
    """[inspect] 定时巡检配置."""
    enabled: bool = False
    interval_seconds: int = 1800
    max_new_tasks_per_round: int = 3
    signals: tuple[str, ...] = ("git_log", "failed_tasks", "todos")
    auto_execute: bool = False
    priority: str = "P3"
    # 留空时走统一 planner 解析：显式参数 / [inspect] / [agents] / codex。
    planner: Optional[str] = None


@dataclass
class ClassifierConfig:
    """[classifier] 意图分类器配置. provider 留空则走本地 codex CLI."""
    provider: str = ""  # API provider key (如 openai-gpt4o / claude-haiku / deepseek) 或空走本地
    model: str = ""  # 可选：覆盖 provider 默认模型
    enabled: bool = True  # 关闭则所有输入直接当需求处理
    timeout: int = 30


@dataclass
class ProviderAPIConfig:
    """单个 Provider 的 API 配置."""
    enabled: bool = True
    api_key: str = ""
    model: str = ""  # 覆盖默认模型
    base_url: str = ""  # 自定义端点
    max_tokens: int = 4096
    temperature: float = 0.7
    auto_model_selection: Optional[bool] = None
    simple_model: str = ""
    complex_model: str = ""
    thinking: str = ""
    reasoning_effort: str = ""


@dataclass
class AgentsConfig:
    """AGENTS.toml 完整配置结构."""
    project: ProjectConfig = field(default_factory=ProjectConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    inspect: InspectConfig = field(default_factory=InspectConfig)
    notifications: dict = field(default_factory=dict)
    feishu_bot: dict = field(default_factory=dict)
    # AI Providers 配置
    providers: dict[str, ProviderAPIConfig] = field(default_factory=dict)
    # 项目级 OpenCode 权限策略；工具品牌/TUI 配置不从项目读取。
    opencode_permission: dict[str, Any] = field(default_factory=dict)

    # 兼容旧格式的别名
    project_name: str = ""
    base_branch: str = "dev"
    default_mode: str = "dual"
    worktree_base: Optional[str] = None
    planner: Optional[str] = None
    builder: Optional[str] = None
    reviewer: Optional[str] = None
    # CLI family 命令名/路径映射；旧版 codex_cmd/claude_cmd 标量已移除。
    commands: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_AGENT_COMMANDS))
    interval_seconds: int = 600
    stale_minutes: int = 30
    webhook_url: str = ""
    webhook_provider: str = "auto"
    webhook_secret: str = ""
    notifications_enabled: bool = False
    feishu_bot_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_node_command: str = "node"
    feishu_default_project: str = ""
    feishu_command_prefix: str = ""
    config_file_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict, config_file_path: Optional[str] = None) -> "AgentsConfig":
        """从字典加载配置。"""
        from codepilot.core.config_builder import build_agents_config_from_dict

        return build_agents_config_from_dict(
            cls,
            data,
            config_file_path=config_file_path,
        )

    def command_for(self, family: str) -> str:
        """Resolve a CLI family name to its configured command/path.

        Falls back to the family name itself when not configured, so callers can
        rely on a non-empty string and ``which``/``shutil.which`` will surface
        the missing-binary case downstream.
        """
        return (self.commands.get(family, "") or family).strip() or family

    def get_provider_api_key(self, provider_name: str) -> Optional[str]:
        """获取 Provider 的 API Key，优先级：配置 > 环境变量."""
        if provider_name in self.providers:
            cfg = self.providers[provider_name]
            if cfg.api_key:
                return cfg.api_key

        # 环境变量映射
        env_keys = {
            "openai": "OPENAI_API_KEY",
            "openai-gpt4": "OPENAI_API_KEY",
            "openai-gpt4o": "OPENAI_API_KEY",
            "openai-gpt35": "OPENAI_API_KEY",
            "claude-opus": "ANTHROPIC_API_KEY",
            "claude-sonnet": "ANTHROPIC_API_KEY",
            "claude-haiku": "ANTHROPIC_API_KEY",
            "hunyuan": "HUNYUAN_API_KEY",
            "zhipu-glm4": "ZHIPU_API_KEY",
            "wenxin": "ERNIE_API_KEY",
            "qwen": "DASHSCOPE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
        }
        env_key = env_keys.get(provider_name)
        if env_key:
            return os.environ.get(env_key)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Provider 环境变量注入（用于 OpenCode 启动时自动注入 API key）
# ═══════════════════════════════════════════════════════════════════════════════

# (api_key_env_var, base_url_env_var_or_none)
PROVIDER_ENV_VAR_MAP: dict[str, tuple[str, str | None]] = {
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openai-gpt4": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openai-gpt4o": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openai-gpt35": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "claude-opus": ("ANTHROPIC_API_KEY", None),
    "claude-sonnet": ("ANTHROPIC_API_KEY", None),
    "claude-haiku": ("ANTHROPIC_API_KEY", None),
    "hunyuan": ("HUNYUAN_API_KEY", None),
    "zhipu-glm4": ("ZHIPU_API_KEY", None),
    "wenxin": ("ERNIE_API_KEY", None),
    "qwen": ("DASHSCOPE_API_KEY", None),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
}


def build_provider_env_vars(config: AgentsConfig) -> dict[str, str]:
    """从已启用的 provider 配置构建环境变量，用于注入 OpenCode 启动环境。

    遍历所有已启用且配置了 api_key 的 provider，映射为对应的环境变量
    （如 OPENAI_API_KEY、ANTHROPIC_API_KEY 等），同时注入 base_url。
    项目级配置优先级高于全局（load_project_config 中已合并）。
    """
    if config is None:
        return {}
    env_vars: dict[str, str] = {}
    for provider_name, provider_cfg in config.providers.items():
        if not provider_cfg.enabled:
            continue
        mapping = PROVIDER_ENV_VAR_MAP.get(provider_name)
        if mapping is None:
            continue
        api_key_env, base_url_env = mapping
        key_value = (provider_cfg.api_key or "").strip()
        if key_value:
            env_vars[api_key_env] = key_value
        if base_url_env:
            url_value = (provider_cfg.base_url or "").strip()
            if url_value:
                env_vars[base_url_env] = url_value
    return env_vars


# ═══════════════════════════════════════════════════════════════════════════════
# 配置查找
# ═══════════════════════════════════════════════════════════════════════════════

def find_config(start_dir: Optional[Path] = None) -> Optional[Path]:
    """
    从 start_dir 向上逐级查找 AGENTS.toml.
    如果 start_dir 为 None，使用当前工作目录.
    返回找到的配置文件路径，未找到返回 None.
    """
    if start_dir is None:
        start_dir = Path.cwd()

    current = start_dir.resolve()
    for _ in range(20):
        config_path = current / CONFIG_FILENAME
        if config_path.is_file():
            return config_path
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _locate_secrets_file(
    config_path: Optional[Path],
    *,
    allow_env_override: bool = True,
) -> Optional[Path]:
    """Find the secrets file that should overlay ``config_path``.

    Search order:

    1. ``$CODEPILOT_SECRETS_PATH`` — explicit override.
    2. ``<AGENTS.toml dir>/.codepilot.secrets.toml`` — sibling of the
       resolved config file (most common case).
    """
    override = os.environ.get(SECRETS_PATH_ENV, "").strip() if allow_env_override else ""
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None

    if config_path is not None:
        sibling = config_path.parent / SECRETS_FILENAME
        if sibling.is_file():
            return sibling

    return None


def _load_toml_dict(path: Path) -> Optional[dict[str, Any]]:
    """Load a TOML file and ensure the top-level object is a dict."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge two config dictionaries; ``override`` wins on conflicts."""
    merged = deepcopy(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(existing, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _overlay_secrets_data(data: dict[str, Any], secrets_path: Path) -> dict[str, Any]:
    """Overlay sensitive values from a secrets TOML onto raw config data."""
    secrets = _load_toml_dict(secrets_path)
    if not secrets:
        return data

    merged = deepcopy(data)

    providers = secrets.get("providers", {})
    if isinstance(providers, dict):
        merged_providers = merged.get("providers")
        if not isinstance(merged_providers, dict):
            merged_providers = {}
            merged["providers"] = merged_providers

        for name, cfg in providers.items():
            if not isinstance(cfg, dict):
                continue
            key = str(cfg.get("api_key", "")).strip()
            if not key:
                continue
            target = merged_providers.get(name)
            if not isinstance(target, dict):
                target = {}
                merged_providers[name] = target
            target["api_key"] = key

    feishu_bot = secrets.get("feishu_bot", {})
    if isinstance(feishu_bot, dict):
        app_secret = str(feishu_bot.get("app_secret", "") or "").strip()
        if app_secret:
            target = merged.get("feishu_bot")
            if not isinstance(target, dict):
                target = {}
                merged["feishu_bot"] = target
            target["app_secret"] = app_secret
    return merged


def _warn_on_inline_secrets_data(data: dict[str, Any], config_path: Path) -> None:
    """Emit a one-line deprecation nudge when AGENTS.toml holds secrets.

    Mixing secrets into the committed AGENTS.toml is the concrete risk
    this refactor addresses; we log (not raise) so existing installs keep
    working while giving users a visible prompt to migrate.
    """
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return

    leaked: list[str] = []
    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        if str(cfg.get("api_key", "")).strip():
            leaked.append(str(name))

    feishu_bot = data.get("feishu_bot", {})
    has_feishu_secret = isinstance(feishu_bot, dict) and bool(str(feishu_bot.get("app_secret", "")).strip())

    if not leaked and not has_feishu_secret:
        return
    try:
        from codepilot.core.logger import get_logger
        if leaked:
            get_logger("config").warning(
                "AGENTS.toml at %s contains inline api_key for %s; "
                "move these to %s (sibling file) to avoid committing secrets.",
                config_path,
                ", ".join(sorted(leaked)),
                SECRETS_FILENAME,
            )
        if has_feishu_secret:
            get_logger("config").warning(
                "AGENTS.toml at %s contains inline feishu_bot.app_secret; "
                "move it to %s (sibling file) to avoid committing secrets.",
                config_path,
                SECRETS_FILENAME,
            )
    except Exception:  # logger setup must never break config load
        pass


def _load_config_data(
    config_path: Path,
    *,
    allow_env_secrets_override: bool = True,
) -> Optional[dict[str, Any]]:
    """Load one config file + corresponding secrets overlay into a raw dict."""
    data = _load_toml_dict(config_path)
    if data is None:
        return None

    _warn_on_inline_secrets_data(data, config_path)
    secrets_path = _locate_secrets_file(
        config_path,
        allow_env_override=allow_env_secrets_override,
    )
    if secrets_path is not None:
        data = _overlay_secrets_data(data, secrets_path)
    return data


def load_config(config_path: Optional[Path] = None) -> Optional[AgentsConfig]:
    """
    加载 AGENTS.toml 配置文件.
    如果 config_path 为 None，自动查找.

    如果同目录存在 ``.codepilot.secrets.toml``，会把其中的
    ``[providers.<name>].api_key`` / ``[feishu_bot].app_secret`` 覆盖到
    返回的配置里，从而让用户可以把敏感字段从 AGENTS.toml 里分离出去
    （建议 gitignore 后者）。
    """
    if config_path is None:
        config_path = find_config()
        if config_path is None:
            config_path = find_global_config()

    if config_path is None or not config_path.is_file():
        return None

    data = _load_config_data(config_path, allow_env_secrets_override=True)
    if data is None:
        return None

    try:
        return AgentsConfig.from_dict(data, config_file_path=str(config_path))
    except ConfigError:
        raise
    except Exception:
        return None


def sanitize_config_for_display(config: AgentsConfig) -> AgentsConfig:
    """Return a copy of ``config`` with all api_key values scrubbed.

    Use whenever the config is about to be serialised for display — logs,
    diagnostic dumps, the ``codepilot doctor`` command — so leaking a
    printed config into a screenshot or support ticket is safe.
    """
    clone = deepcopy(config)
    for provider_cfg in clone.providers.values():
        if provider_cfg.api_key:
            provider_cfg.api_key = "***"
    if isinstance(clone.feishu_bot, dict) and str(clone.feishu_bot.get("app_secret", "")).strip():
        clone.feishu_bot["app_secret"] = "***"
    if clone.feishu_app_secret:
        clone.feishu_app_secret = "***"
    return clone


def resolve_planner(
    config: Optional[AgentsConfig],
    scope: str = "automation",
    *,
    explicit: Optional[str] = None,
) -> str:
    """Resolve the effective planner with a shared precedence order.

    Resolution order:
    1. Explicit CLI/runtime override
    2. Scope-specific config (currently ``automation`` / ``inspect``)
    3. ``[agents].planner``
    4. Built-in fallback ``codex``
    """
    explicit_planner = _normalize_optional_agent_name(explicit)
    if explicit_planner:
        return explicit_planner

    if config:
        normalized_scope = (scope or "automation").strip().lower()
        if normalized_scope == "inspect":
            inspect_planner = _normalize_optional_agent_name(
                getattr(getattr(config, "inspect", None), "planner", None)
            )
            if inspect_planner:
                return inspect_planner
        elif normalized_scope == "automation":
            automation_planner = _normalize_optional_agent_name(
                getattr(getattr(config, "automation", None), "planner", None)
            )
            if automation_planner:
                return automation_planner

        agents_planner = _normalize_optional_agent_name(getattr(config, "planner", None))
        if agents_planner:
            return agents_planner

    return "codex"


def resolve_project_config_inputs(
    project_ref: Optional[str | Path | Mapping[str, Any]] = None,
    *,
    config_file: Optional[str | Path] = None,
) -> tuple[Optional[str | Path], Optional[str | Path]]:
    """Normalize a project path/file reference into config-resolution inputs.

    Callers often already hold the DB project row, where ``config_file`` is the
    authoritative project-level override. This helper is the single place that
    turns that row into ``(project_path, config_file)`` so runtime entry points
    do not accidentally ignore the registered override and rediscover a nearby
    ``AGENTS.toml`` instead.
    """
    if isinstance(project_ref, Mapping):
        explicit_config = project_ref.get("config_file") or None
        project_path = project_ref.get("path") or None
        return project_path, config_file or explicit_config
    return project_ref, config_file


def resolve_project_config_reference(
    project_ref: Optional[str | Path | Mapping[str, Any]] = None,
    *,
    config_file: Optional[str | Path] = None,
) -> Optional[str | Path]:
    """Return the best reference to use for provider/config lookup."""
    project_path, resolved_config_file = resolve_project_config_inputs(
        project_ref,
        config_file=config_file,
    )
    if resolved_config_file:
        candidate = Path(resolved_config_file).expanduser()
        if candidate.is_file() or candidate.is_dir():
            return resolved_config_file
    return project_path


def resolve_config_path(
    project_path: Optional[str | Path | Mapping[str, Any]] = None,
    *,
    config_file: Optional[str | Path] = None,
) -> Optional[Path]:
    """Resolve the effective AGENTS.toml path from an explicit file or project path."""
    project_path, config_file = resolve_project_config_inputs(
        project_path,
        config_file=config_file,
    )
    if config_file:
        candidate = Path(config_file).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        if candidate.is_dir():
            direct = candidate / CONFIG_FILENAME
            if direct.is_file():
                return direct.resolve()

    if project_path is None:
        return find_config()

    candidate = Path(project_path).expanduser()
    if candidate.is_file():
        return candidate.resolve()

    direct = candidate / CONFIG_FILENAME
    if direct.is_file():
        return direct.resolve()

    return find_config(candidate)


def load_project_config(
    project_path: Optional[str | Path | Mapping[str, Any]] = None,
    *,
    config_file: Optional[str | Path] = None,
) -> Optional[AgentsConfig]:
    """Load effective config with precedence: global defaults < project overrides."""
    project_path, config_file = resolve_project_config_inputs(
        project_path,
        config_file=config_file,
    )
    resolved_local = resolve_config_path(project_path, config_file=config_file)
    global_path = find_global_config()

    # Avoid merging the same file twice when project config *is* the global config.
    if resolved_local and global_path and resolved_local.resolve() == global_path.resolve():
        global_path = None

    merged: dict[str, Any] = {}
    loaded_any = False

    if global_path is not None:
        global_data = _load_config_data(global_path, allow_env_secrets_override=False)
        if global_data is not None:
            merged = _merge_dicts(merged, global_data)
            loaded_any = True

    if resolved_local is not None and resolved_local.is_file():
        local_data = _load_config_data(resolved_local, allow_env_secrets_override=True)
        if local_data is not None:
            merged = _merge_dicts(merged, local_data)
            loaded_any = True

    if not loaded_any:
        return None

    config_file_path = (
        str(resolved_local.resolve())
        if resolved_local is not None and resolved_local.is_file()
        else (str(global_path.resolve()) if global_path is not None else None)
    )

    try:
        config = AgentsConfig.from_dict(merged, config_file_path=config_file_path)
    except ConfigError:
        raise
    except Exception:
        return None

    # Global config is cross-project by design; keep project name/path local when absent.
    if project_path is not None:
        project_data = merged.get("project", {})
        has_project_name = isinstance(project_data, dict) and bool(str(project_data.get("name", "")).strip())
        if not has_project_name:
            project_name = Path(project_path).expanduser().name.strip()
            if project_name:
                config.project.name = project_name
                config.project_name = project_name

    return config


def find_project_root(config_path: Optional[Path] = None) -> Optional[Path]:
    """根据配置文件路径找到项目根目录."""
    if config_path is None:
        config_path = find_config()
    if config_path:
        return config_path.parent
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 默认 AGENTS.toml 模板（扩展版）
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_TEMPLATE = """\
# CodePilot 项目配置文件
# 支持多种 AI Provider（CLI 和 API）

[project]
# 项目标识名（用于 codepilot 命令 --project 参数）
name = "{name}"
# Git 主分支
base_branch = "dev"
# 兼容字段：默认任务智能体；自动规划执行优先使用 [automation].task_agent
default_mode = "dual"
# Worktree 隔离目录，空值则自动推导到 ~/.codepilot/data/<project>/worktrees/
worktree_base = ""

[shell]
# Shell 配置（跨平台）
# preferred: 自动检测 (auto) / powershell / pwsh / bash / zsh
preferred = "auto"
# 自定义 Shell 路径（可选）
# powershell_path = "C:\\\\Program Files\\\\PowerShell\\\\7\\\\pwsh.exe"
# bash_path = "/usr/local/bin/bash"

[agents]
# 通用规划器；留空时使用场景默认值
planner = ""
# dual 模式 builder；留空时默认 codex
builder = ""
# dual 模式 reviewer；留空时默认 claude
reviewer = ""

[agents.commands]
# CLI family -> 命令名/绝对路径（cp-opencode 为项目自带的 OpenCode 包装器）。
claude = "claude"
codex = "codex"
opencode = "cp-opencode"

[opencode.permission]
# 项目级 OpenCode 权限策略：
# ask = 默认逐项确认；full_access = 所有 OpenCode 工具/MCP 调用直接允许；custom = 使用下方规则
mode = "ask"
# 自定义示例：
# mode = "custom"
# "*" = "ask"
# bash = "allow"
# edit = "ask"
# write = "deny"
# webfetch = "allow"

[dispatch]
# task-dispatch 脚本路径（留空时先查 ~/.codepilot/data/<project>/scripts/，再查包内置脚本）
dispatch_path = ""
# 调度间隔（秒）
interval_seconds = 600
# 任务过期时间（分钟），超时未完成自动重置
stale_minutes = 30

[automation]
# 纯文本需求模式默认使用 Codex 规划，规划出的任务默认交给 task_agent 执行
planner = "codex"
# chat 默认启动的 MCP agent；留空时使用 opencode
default_agent = ""
# 自动规划出来的任务默认交给哪个执行智能体
task_agent = "dual"
executor = "builtin"
# 输入需求后是否直接开始执行
auto_execute = true
# 若为 true，则规划完成后先确认再执行
confirm_before_execute = false
# 内置执行器成功后是否自动提交
auto_commit = true
# 复杂需求最多拆分出的子任务数
max_tasks = 5
# 每个子任务失败后的最大重试次数
max_retries = 3
# 是否为任务创建独立执行分支；false 时直接在当前工作区执行
per_task_branch = true
# 任务执行工作区：
# direct = 直接在项目工作目录/base_branch 开发
# branch = 在项目工作目录创建任务分支，完成后合并并删除
# worktree = 使用独立临时 worktree
# 缺失或配置错误时默认使用 branch
task_workspace = "branch"
# 复制到任务 worktree 的本地上下文。PHP/Composer 项目可加入 "vendor"，避免
# Composer autoloader 的 baseDir 指向主工作区或已删除 worktree。
worktree_context_patterns = [".env*"]
# 链接到任务 worktree 的依赖目录。Composer vendor 不建议放在这里。
worktree_context_link_patterns = ["node_modules", ".venv", "venv", "env", ".tox", ".nox", ".gradle", "target", "build", "cmake-build-*", ".dart_tool", "Pods", "Carthage", ".terraform", ".serverless"]
# 执行预检发现主工作区已有未提交改动时的处理策略：
# stop = 停止执行（默认）；commit = 分析状态后提交；stash = stash 并写入日志记录
preflight_dirty_worktree = "stop"
# 规划前先做代码侦察，再拆任务
two_stage_planning = true
# Builder/Reviewer 闭环最大轮数
max_review_rounds = 2
# 子进程连续多少秒没有新输出就认为卡死并终止；0 表示关闭
agent_silence_timeout_seconds = 0
# workflow next --auto 自动推进策略；默认只执行低风险 plan/ignore，不创建或导入任务
workflow_auto_create_inspect_tasks = false
workflow_auto_import_plan_tasks = false
workflow_auto_max_steps = 1
workflow_auto_failure_threshold = 1
# 文本模式 CLI 兜底顺序；前面项不可用时按顺序退到下一个。
fallback_cli_order = ["claude", "codex", "opencode"]
# 智能体 prompt / 任务内容 / 输出语言偏好：en 或 zh-CN；默认 en。
agent_language = "en"

[automation.scheduled_agents.task_health]
enabled = true
agent = "codex"
interval = "10m"
prompt = "Review local CodePilot task status, failed tasks, long-running in-progress tasks, and recent scheduler audit records. Summarize risks only and suggest conservative checks."
max_cost_usd = 0.1
max_daily_cost_usd = 0.5

[automation.scheduled_agents.daily_summary]
enabled = true
agent = "codex"
interval = "1d"
prompt = "Prepare a concise daily project summary draft for Feishu. Include task movement, failed or blocked work, and conservative next checks without sending any message."
max_cost_usd = 0.1
max_daily_cost_usd = 0.5

[automation.scheduled_agents.auto_inspect]
enabled = true
agent = "codex"
interval = "30m"
prompt = "Review configured inspect signals and propose a lightweight inspection plan with actionable findings. Do not modify files or execute fixes."
max_cost_usd = 0.1
max_daily_cost_usd = 0.5

[automation.event_agents.failed_task_triage]
enabled = false
trigger = "task.failed"
agent = "codex"
prompt = "Task {{{{ task_id }}}} failed with {{{{ error_message }}}}. Suggest the smallest repair."
max_cost_usd = 0.1
max_daily_cost_usd = 0.5

[inspect]
# 定时巡检配置
enabled = false
interval_seconds = 1800
max_new_tasks_per_round = 3
signals = ["git_log", "failed_tasks", "todos"]
# 可选信号：deps、ruff、pytest、code_metrics。默认保持轻量，项目可按需启用。
auto_execute = false
priority = "P3"
# 巡检专用 planner；留空时回退到 [agents].planner / codex
planner = ""

[providers]
# AI Provider API 配置（可选，不配置则使用环境变量）
# base_url 可选；用于 OpenAI/Anthropic 兼容代理或自定义接口地址

# OpenAI
# [providers.openai-gpt4o]
# enabled = true
# model = "gpt-4o"
# base_url = ""  # 例如 "https://your-proxy.example/v1"
# api_key = ""  # 或设置环境变量 OPENAI_API_KEY
# max_tokens = 4096
# temperature = 0.7

# Claude (Anthropic)
# [providers.claude-sonnet]
# enabled = true
# model = "claude-3-5-sonnet-20241022"
# base_url = ""  # 例如 "https://your-anthropic-proxy.example"
# api_key = ""  # 或设置环境变量 ANTHROPIC_API_KEY
# max_tokens = 4096
# temperature = 0.7

# 腾讯云混元
# [providers.hunyuan]
# enabled = false
# model = "hunyuan"
# base_url = "https://hunyuan.cloud.tencent.com"
# api_key = ""  # 设置环境变量 HUNYUAN_API_KEY
# max_tokens = 4096
# temperature = 0.7

# 阿里通义千问
# [providers.qwen]
# enabled = false
# model = "qwen-plus"
# base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# api_key = ""  # 设置环境变量 DASHSCOPE_API_KEY
# max_tokens = 4096
# temperature = 0.7

# DeepSeek
# [providers.deepseek]
# enabled = false
# model = ""  # 显式单模型覆盖；自动切换时留空
# auto_model_selection = true
# simple_model = "deepseek-v4-flash"
# complex_model = "deepseek-v4-pro"
# thinking = "auto"  # auto / enabled / disabled
# reasoning_effort = "auto"  # auto / high / max
# base_url = "https://api.deepseek.com"
# api_key = ""  # 设置环境变量 DEEPSEEK_API_KEY
# max_tokens = 8192
# temperature = 0.7

# Ollama (本地)
# [providers.ollama]
# enabled = false
# model = "llama3"
# base_url = "http://localhost:11434/v1"
# api_key = ""
# max_tokens = 4096
# temperature = 0.7

[notifications]
# 任务状态通知 Webhook URL
webhook_url = ""
# 通知类型：auto / feishu / wecom / generic
provider = "auto"
# 飞书机器人签名密钥（未开启签名校验时留空）
webhook_secret = ""
enabled = false

[feishu_bot]
# 飞书企业应用长连接机器人（本地可用，无需公网回调）
enabled = false
app_id = ""
# App Secret 只写到同目录 .codepilot.secrets.toml，避免 AGENTS.toml 出现密钥字段:
# [feishu_bot]
node_command = "node"
default_project = ""
command_prefix = ""
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 兼容旧版本的简化配置模板
# ═══════════════════════════════════════════════════════════════════════════════

LEGACY_TEMPLATE = """\
# CodePilot 项目配置文件（简化版）
# 此文件可选；工具也可通过数据库管理项目

[project]
name = "{name}"
base_branch = "dev"
default_mode = "dual"
worktree_base = ""

[agents.commands]
claude = "claude"
codex = "codex"
opencode = "cp-opencode"

[dispatch]
dispatch_path = ""
interval_seconds = 600
stale_minutes = 30

[automation]
planner = "codex"
default_agent = ""
executor = "builtin"
auto_execute = true
confirm_before_execute = false
auto_commit = true
max_tasks = 5
max_retries = 3
workflow_auto_create_inspect_tasks = false
workflow_auto_import_plan_tasks = false
workflow_auto_max_steps = 1
workflow_auto_failure_threshold = 1

[notifications]
webhook_url = ""
provider = "auto"
webhook_secret = ""
enabled = false

[feishu_bot]
enabled = false
app_id = ""
# App Secret 只写到同目录 .codepilot.secrets.toml，避免 AGENTS.toml 出现密钥字段:
# [feishu_bot]
node_command = "node"
default_project = ""
command_prefix = ""
"""
