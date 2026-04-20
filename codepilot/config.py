"""AGENTS.toml 配置发现与解析（扩展版，支持多 AI Provider）."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


CONFIG_FILENAME = "AGENTS.toml"
SECRETS_FILENAME = ".codepilot.secrets.toml"  # sibling file; never commit

# Preferred env var holding a secrets file path. When set, it overrides
# the default sibling-file discovery — useful for CI / containerised runs
# where the secrets file lives outside the repo.
SECRETS_PATH_ENV = "CODEPILOT_SECRETS_PATH"


def _normalize_optional_agent_name(value: object) -> Optional[str]:
    """Normalize an optional agent name from ``[agents]`` config values."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    from codepilot.ai import normalize_agent_name

    normalized = normalize_agent_name(text).strip()
    return normalized or None


# ═══════════════════════════════════════════════════════════════════════════════
# 配置数据模型
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ProjectConfig:
    """[project] 项目配置."""
    name: str = ""
    base_branch: str = "dev"
    default_mode: str = "codex"
    worktree_base: Optional[str] = None


@dataclass
class ShellConfig:
    """Shell 配置（跨平台）."""
    preferred: str = ""  # 自动检测
    powershell_path: Optional[str] = None  # 自定义 PowerShell 路径
    bash_path: Optional[str] = None


@dataclass
class DispatchConfig:
    """[dispatch] 调度配置."""
    dispatch_path: str = ""
    interval_seconds: int = 600
    stale_minutes: int = 30


@dataclass
class AutomationConfig:
    """[automation] 自动规划和执行配置."""
    planner: str = "codex"
    executor: str = "builtin"
    auto_execute: bool = True
    confirm_before_execute: bool = False
    auto_commit: bool = True
    max_tasks: int = 5
    max_retries: int = 3
    per_task_branch: bool = True
    # 两阶段规划：先让 planner 读代码（侦察），再拆任务。关闭后变回一次性规划。
    two_stage_planning: bool = True
    # 需求不具体时主动反问澄清。关闭后遇到模糊需求直接硬拆。
    clarify_vague_requirements: bool = True
    # 一次规划最多反问多少轮。用户答到这个上限后强制进规划。
    clarify_max_turns: int = 3
    # Builder-Reviewer 闭环最大轮数。reviewer 判 FAIL 时, builder 拿 reviewer
    # 反馈再做一次, 循环最多这么多轮。设为 1 等于关闭闭环（老行为）。
    max_review_rounds: int = 2


@dataclass
class InspectConfig:
    """[inspect] 定时巡检配置."""
    enabled: bool = False
    interval_seconds: int = 1800
    max_new_tasks_per_round: int = 3
    signals: tuple[str, ...] = ("git_log", "failed_tasks", "todos")
    auto_execute: bool = False
    priority: str = "P3"
    # 巡检默认用 claude（推理快、适合分析），写代码走 codex 交由 --agent 决定。
    planner: str = "claude"


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

    # AI Providers 配置
    providers: dict[str, ProviderAPIConfig] = field(default_factory=dict)

    # 兼容旧格式的别名
    project_name: str = ""
    base_branch: str = "dev"
    default_mode: str = "codex"
    worktree_base: Optional[str] = None
    builder: Optional[str] = None
    reviewer: Optional[str] = None
    codex_cmd: str = "codex"
    claude_cmd: str = "claude"
    interval_seconds: int = 600
    stale_minutes: int = 30
    webhook_url: str = ""
    notifications_enabled: bool = False
    config_file_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict, config_file_path: Optional[str] = None) -> "AgentsConfig":
        """从字典加载配置."""
        proj = data.get("project", {})
        agents = data.get("agents", {})
        dispatch = data.get("dispatch", {})
        automation = data.get("automation", {})
        classifier = data.get("classifier", {})
        inspect = data.get("inspect", {})
        notifications = data.get("notifications", {})
        shell = data.get("shell", {})
        providers = data.get("providers", {})

        # 解析 providers
        providers_config = {}
        for name, cfg in providers.items():
            if isinstance(cfg, dict):
                providers_config[name] = ProviderAPIConfig(
                    enabled=cfg.get("enabled", True),
                    api_key=cfg.get("api_key", ""),
                    model=cfg.get("model", ""),
                    base_url=cfg.get("base_url", ""),
                    max_tokens=cfg.get("max_tokens", 4096),
                    temperature=cfg.get("temperature", 0.7),
                )
            else:
                providers_config[name] = ProviderAPIConfig(enabled=bool(cfg))

        return cls(
            project=ProjectConfig(
                name=proj.get("name", ""),
                base_branch=proj.get("base_branch", "dev"),
                default_mode=proj.get("default_mode", "codex"),
                worktree_base=proj.get("worktree_base"),
            ),
            shell=ShellConfig(
                preferred=shell.get("preferred", ""),
                powershell_path=shell.get("powershell_path"),
                bash_path=shell.get("bash_path"),
            ),
            dispatch=DispatchConfig(
                dispatch_path=dispatch.get("dispatch_path", ""),
                interval_seconds=dispatch.get("interval_seconds", 600),
                stale_minutes=dispatch.get("stale_minutes", 30),
            ),
            automation=AutomationConfig(
                planner=automation.get("planner", "codex"),
                executor=automation.get("executor", "builtin"),
                auto_execute=automation.get("auto_execute", True),
                confirm_before_execute=automation.get("confirm_before_execute", False),
                auto_commit=automation.get("auto_commit", True),
                max_tasks=automation.get("max_tasks", 5),
                max_retries=automation.get("max_retries", 3),
                per_task_branch=automation.get("per_task_branch", True),
                two_stage_planning=automation.get("two_stage_planning", True),
                clarify_vague_requirements=automation.get("clarify_vague_requirements", True),
                clarify_max_turns=automation.get("clarify_max_turns", 3),
                max_review_rounds=automation.get("max_review_rounds", 2),
            ),
            classifier=ClassifierConfig(
                provider=classifier.get("provider", ""),
                model=classifier.get("model", ""),
                enabled=classifier.get("enabled", True),
                timeout=classifier.get("timeout", 30),
            ),
            inspect=InspectConfig(
                enabled=inspect.get("enabled", False),
                interval_seconds=inspect.get("interval_seconds", 1800),
                max_new_tasks_per_round=inspect.get("max_new_tasks_per_round", 3),
                signals=tuple(inspect.get("signals", ["git_log", "failed_tasks", "todos"])),
                auto_execute=inspect.get("auto_execute", False),
                planner=inspect.get("planner", "claude"),
                priority=inspect.get("priority", "P3"),
            ),
            notifications=notifications,
            providers=providers_config,
            # 兼容字段
            project_name=proj.get("name", ""),
            base_branch=proj.get("base_branch", "dev"),
            default_mode=proj.get("default_mode", "codex"),
            worktree_base=proj.get("worktree_base"),
            builder=_normalize_optional_agent_name(agents.get("builder")),
            reviewer=_normalize_optional_agent_name(agents.get("reviewer")),
            codex_cmd=agents.get("codex_cmd", "codex"),
            claude_cmd=agents.get("claude_cmd", "claude"),
            interval_seconds=dispatch.get("interval_seconds", 600),
            stale_minutes=dispatch.get("stale_minutes", 30),
            webhook_url=notifications.get("webhook_url", ""),
            notifications_enabled=notifications.get("enabled", False),
            config_file_path=config_file_path,
        )

    def get_provider_api_key(self, provider_name: str) -> Optional[str]:
        """获取 Provider 的 API Key，优先级：配置 > 环境变量."""
        if provider_name in self.providers:
            cfg = self.providers[provider_name]
            if cfg.api_key:
                return cfg.api_key

        # 环境变量映射
        env_keys = {
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


def _locate_secrets_file(config_path: Optional[Path]) -> Optional[Path]:
    """Find the secrets file that should overlay ``config_path``.

    Search order:

    1. ``$CODEPILOT_SECRETS_PATH`` — explicit override.
    2. ``<AGENTS.toml dir>/.codepilot.secrets.toml`` — sibling of the
       resolved config file (most common case).
    3. ``~/.codepilot/.codepilot.secrets.toml`` — user-global fallback
       for users who don't want per-project secret files.
    """
    override = os.environ.get(SECRETS_PATH_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None

    if config_path is not None:
        sibling = config_path.parent / SECRETS_FILENAME
        if sibling.is_file():
            return sibling

    global_file = Path.home() / ".codepilot" / SECRETS_FILENAME
    return global_file if global_file.is_file() else None


def _overlay_secrets(config: AgentsConfig, secrets_path: Path) -> None:
    """Merge ``[providers.<name>]`` api_key entries from a secrets file.

    Only the ``api_key`` field is read — other keys in the secrets file
    are ignored so users can't accidentally override model or endpoint
    settings from a file that shouldn't hold configuration choices.
    """
    try:
        with open(secrets_path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return

    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return

    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        key = str(cfg.get("api_key", "")).strip()
        if not key:
            continue
        if name not in config.providers:
            config.providers[name] = ProviderAPIConfig(api_key=key)
        else:
            # Only overwrite api_key; keep the rest from AGENTS.toml.
            config.providers[name].api_key = key


def _warn_on_inline_secrets(config: AgentsConfig, config_path: Path) -> None:
    """Emit a one-line deprecation nudge when AGENTS.toml holds secrets.

    Mixing secrets into the committed AGENTS.toml is the concrete risk
    this refactor addresses; we log (not raise) so existing installs keep
    working while giving users a visible prompt to migrate.
    """
    leaked = [name for name, cfg in config.providers.items() if cfg.api_key]
    if not leaked:
        return
    try:
        from codepilot.logger import get_logger
        get_logger("config").warning(
            "AGENTS.toml at %s contains inline api_key for %s; "
            "move these to %s (sibling file) to avoid committing secrets.",
            config_path,
            ", ".join(sorted(leaked)),
            SECRETS_FILENAME,
        )
    except Exception:  # logger setup must never break config load
        pass


def load_config(config_path: Optional[Path] = None) -> Optional[AgentsConfig]:
    """
    加载 AGENTS.toml 配置文件.
    如果 config_path 为 None，自动查找.

    如果同目录存在 ``.codepilot.secrets.toml``，会把其中的
    ``[providers.<name>].api_key`` 覆盖到返回的配置里，从而让用户可以
    把敏感字段从 AGENTS.toml 里分离出去（建议 gitignore 后者）。
    """
    if config_path is None:
        config_path = find_config()

    if config_path is None or not config_path.is_file():
        return None

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        config = AgentsConfig.from_dict(data, config_file_path=str(config_path))
    except Exception:
        return None

    _warn_on_inline_secrets(config, config_path)

    secrets_path = _locate_secrets_file(config_path)
    if secrets_path is not None:
        _overlay_secrets(config, secrets_path)

    return config


def sanitize_config_for_display(config: AgentsConfig) -> AgentsConfig:
    """Return a copy of ``config`` with all api_key values scrubbed.

    Use whenever the config is about to be serialised for display — logs,
    diagnostic dumps, the ``codepilot doctor`` command — so leaking a
    printed config into a screenshot or support ticket is safe.
    """
    import copy

    clone = copy.deepcopy(config)
    for provider_cfg in clone.providers.values():
        if provider_cfg.api_key:
            provider_cfg.api_key = "***"
    return clone


def resolve_config_path(
    project_path: Optional[str | Path] = None,
    *,
    config_file: Optional[str | Path] = None,
) -> Optional[Path]:
    """Resolve the effective AGENTS.toml path from an explicit file or project path."""
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
    project_path: Optional[str | Path] = None,
    *,
    config_file: Optional[str | Path] = None,
) -> Optional[AgentsConfig]:
    """Load AGENTS.toml using either a stored config file path or a project root."""
    resolved = resolve_config_path(project_path, config_file=config_file)
    return load_config(resolved) if resolved else None


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
# 默认任务智能体
default_mode = "codex"
# Worktree 隔离目录，空值则自动推导到 ~/.codepilot/worktrees/<project>/
worktree_base = ""

[shell]
# Shell 配置（跨平台）
# preferred: 自动检测 (auto) / powershell / pwsh / bash / zsh
preferred = "auto"
# 自定义 Shell 路径（可选）
# powershell_path = "C:\\\\Program Files\\\\PowerShell\\\\7\\\\pwsh.exe"
# bash_path = "/usr/local/bin/bash"

[agents]
# CLI Agent 命令配置
codex_cmd = "codex"
claude_cmd = "claude"

[dispatch]
# task-dispatch 脚本路径（默认从 codepilot 包内查找）
dispatch_path = ""
# 调度间隔（秒）
interval_seconds = 600
# 任务过期时间（分钟），超时未完成自动重置
stale_minutes = 30

[automation]
# 纯文本需求模式默认使用 Codex 规划和执行
planner = "codex"
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

[providers]
# AI Provider API 配置（可选，不配置则使用环境变量）

# OpenAI
# [providers.openai]
# enabled = true
# model = "gpt-4-turbo-preview"
# api_key = ""  # 或设置环境变量 OPENAI_API_KEY

# Claude (Anthropic)
# [providers.claude]
# enabled = true
# model = "claude-3-5-sonnet-20241022"
# api_key = ""  # 或设置环境变量 ANTHROPIC_API_KEY

# 腾讯云混元
# [providers.hunyuan]
# enabled = false
# api_key = ""  # 设置环境变量 HUNYUAN_API_KEY

# 阿里通义千问
# [providers.qwen]
# enabled = false
# api_key = ""  # 设置环境变量 DASHSCOPE_API_KEY

# DeepSeek
# [providers.deepseek]
# enabled = false
# api_key = ""  # 设置环境变量 DEEPSEEK_API_KEY

# Ollama (本地)
# [providers.ollama]
# enabled = false
# base_url = "http://localhost:11434/v1"
# model = "llama3"

[notifications]
# 飞书/企微 Webhook URL
webhook_url = ""
enabled = false
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
default_mode = "codex"
worktree_base = ""

[agents]
codex_cmd = "codex"
claude_cmd = "claude"

[dispatch]
dispatch_path = ""
interval_seconds = 600
stale_minutes = 30

[automation]
planner = "codex"
executor = "builtin"
auto_execute = true
confirm_before_execute = false
auto_commit = true
max_tasks = 5
max_retries = 3

[notifications]
webhook_url = ""
enabled = false
"""
