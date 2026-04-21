"""AGENTS.toml 配置发现与解析（扩展版，支持多 AI Provider）."""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
GLOBAL_CONFIG_PATH_ENV = "CODEPILOT_GLOBAL_CONFIG_PATH"


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


def resolve_global_config_path() -> Path:
    """Return the global AGENTS.toml path under the CodePilot root."""
    override = os.environ.get(GLOBAL_CONFIG_PATH_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            return (candidate / CONFIG_FILENAME).resolve()
        return candidate.resolve()

    from codepilot.paths import global_storage_root

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
class AutomationConfig:
    """[automation] 自动规划和执行配置."""
    planner: str = "codex"
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
    # 两阶段规划：先让 planner 读代码（侦察），再拆任务。关闭后变回一次性规划。
    two_stage_planning: bool = True
    # 需求不具体时主动反问澄清。关闭后遇到模糊需求直接硬拆。
    clarify_vague_requirements: bool = True
    # 一次规划最多反问多少轮。用户答到这个上限后强制进规划。
    clarify_max_turns: int = 3
    # Builder-Reviewer 闭环最大轮数。reviewer 判 FAIL 时, builder 拿 reviewer
    # 反馈再做一次, 循环最多这么多轮。设为 1 等于关闭闭环（老行为）。
    max_review_rounds: int = 2
    # 子进程 (codex / claude CLI) 连续多少秒没有新输出就认为卡死并 kill，
    # 0 表示关闭该保护。默认关闭以避免误杀慢任务；运维 daemon 可以按需开启。
    agent_silence_timeout_seconds: int = 0


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
    default_mode: str = "dual"
    worktree_base: Optional[str] = None
    planner: Optional[str] = None
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
                default_mode=proj.get("default_mode", "dual"),
                worktree_base=proj.get("worktree_base"),
            ),
            shell=ShellConfig(
                preferred=shell.get("preferred", "auto"),
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
                task_agent=automation.get("task_agent", "dual"),
                executor=automation.get("executor", "builtin"),
                auto_execute=automation.get("auto_execute", True),
                confirm_before_execute=automation.get("confirm_before_execute", False),
                auto_commit=automation.get("auto_commit", True),
                max_tasks=automation.get("max_tasks", 5),
                max_retries=automation.get("max_retries", 3),
                per_task_branch=automation.get("per_task_branch", True),
                task_workspace=automation.get("task_workspace", "branch"),
                two_stage_planning=automation.get("two_stage_planning", True),
                clarify_vague_requirements=automation.get("clarify_vague_requirements", True),
                clarify_max_turns=automation.get("clarify_max_turns", 3),
                max_review_rounds=automation.get("max_review_rounds", 2),
                agent_silence_timeout_seconds=automation.get("agent_silence_timeout_seconds", 0),
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
                planner=_normalize_optional_agent_name(inspect.get("planner")),
                priority=inspect.get("priority", "P3"),
            ),
            notifications=notifications,
            providers=providers_config,
            # 兼容字段
            project_name=proj.get("name", ""),
            base_branch=proj.get("base_branch", "dev"),
            default_mode=proj.get("default_mode", "dual"),
            worktree_base=proj.get("worktree_base"),
            planner=_normalize_optional_agent_name(agents.get("planner")),
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
    """Overlay provider ``api_key`` values from a secrets TOML onto raw data."""
    secrets = _load_toml_dict(secrets_path)
    if not secrets:
        return data

    providers = secrets.get("providers", {})
    if not isinstance(providers, dict):
        return data

    merged = deepcopy(data)
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
    ``[providers.<name>].api_key`` 覆盖到返回的配置里，从而让用户可以
    把敏感字段从 AGENTS.toml 里分离出去（建议 gitignore 后者）。
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
    """Load effective config with precedence: global defaults < project overrides."""
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
# CLI Agent 命令配置
codex_cmd = "codex"
claude_cmd = "claude"
# 通用规划器；留空时使用场景默认值
planner = ""
# dual 模式 builder；留空时默认 codex
builder = ""
# dual 模式 reviewer；留空时默认 claude
reviewer = ""

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
# 规划前先做代码侦察，再拆任务
two_stage_planning = true
# 需求模糊时先反问澄清
clarify_vague_requirements = true
# 最多澄清轮数
clarify_max_turns = 3
# Builder/Reviewer 闭环最大轮数
max_review_rounds = 2
# 子进程连续多少秒没有新输出就认为卡死并终止；0 表示关闭
agent_silence_timeout_seconds = 0

[classifier]
# 意图分类器配置；provider 留空则走本地 CLI 兜底
provider = ""
model = ""
enabled = true
timeout = 30

[inspect]
# 定时巡检配置
enabled = false
interval_seconds = 1800
max_new_tasks_per_round = 3
signals = ["git_log", "failed_tasks", "todos"]
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
# model = "deepseek-chat"
# base_url = "https://api.deepseek.com"
# api_key = ""  # 设置环境变量 DEEPSEEK_API_KEY
# max_tokens = 4096
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
default_mode = "dual"
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
