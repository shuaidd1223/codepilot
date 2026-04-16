"""AI 任务内容生成模块。支持 CLI 和 API 两种调用方式。"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from codepilot.config import load_project_config

# ═══════════════════════════════════════════════════════════════════════════════
# Stub 注入钩子（供 e2e 测试使用）
# ═══════════════════════════════════════════════════════════════════════════════
# 设置后，_run_builtin_phase 会调用此函数代替真实 CLI，
# 签名: (task: dict, project_path: Path, phase: str, prompt: str) -> tuple[str, int, str]
_phase_stub: Optional[callable] = None

# API 支持库（可选导入）
try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════════
# 配置模型
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CLIProvider:
    """命令行 AI Provider 配置."""
    name: str
    cmd: str  # 命令名或完整路径
    args_template: list[str] = field(default_factory=list)  # 如 ["-p", "{prompt}"]
    env_prepend: dict = field(default_factory=dict)  # 额外的环境变量
    timeout: int = 180

    def find_executable(self) -> Optional[Path]:
        """查找可执行文件."""
        # 完整路径
        if Path(self.cmd).exists():
            return Path(self.cmd)
        # PATH 中查找
        found = shutil.which(self.cmd)
        return Path(found) if found else None


@dataclass
class APIProvider:
    """API AI Provider 配置."""
    name: str
    provider_type: str  # "openai" | "anthropic" | "custom"
    model: str
    api_key: str = ""
    base_url: str = ""  # 自定义 API 端点
    max_tokens: int = 4096
    temperature: float = 0.7
    api_env_vars: tuple[str, ...] = ()

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
        """构建 API 客户端."""
        api_key = self.resolve_api_key()

        if self.provider_type == "openai":
            if not OPENAI_AVAILABLE:
                raise RuntimeError(
                    f"当前无法使用 {self.name}，因为本机没有安装 openai 依赖。"
                    "请先执行: pip install openai"
                )
            if self.requires_api_key() and not api_key:
                env_names = " / ".join(self.api_env_vars) or "对应的 API Key 环境变量"
                raise RuntimeError(
                    f"当前无法使用 {self.name}，因为还没有配置 API Key。"
                    f"请先设置 {env_names}。"
                )
            client = openai.OpenAI(
                api_key=api_key,
                base_url=self.base_url or None,
            )
            return client, "chat.completions"
        elif self.provider_type == "anthropic":
            if not ANTHROPIC_AVAILABLE:
                raise RuntimeError(
                    f"当前无法使用 {self.name}，因为本机没有安装 anthropic 依赖。"
                    "请先执行: pip install anthropic"
                )
            if self.requires_api_key() and not api_key:
                env_names = " / ".join(self.api_env_vars) or "对应的 API Key 环境变量"
                raise RuntimeError(
                    f"当前无法使用 {self.name}，因为还没有配置 API Key。"
                    f"请先设置 {env_names}。"
                )
            client = anthropic.Anthropic(
                api_key=api_key,
                base_url=self.base_url or None,
            )
            return client, "messages"
        else:
            raise ValueError(f"不支持的 provider_type: {self.provider_type}")


# ═══════════════════════════════════════════════════════════════════════════════
# 内置 Provider 注册表
# ═══════════════════════════════════════════════════════════════════════════════

# CLI Providers（命令行方式）
CLI_PROVIDERS: dict[str, CLIProvider] = {
    # Claude Code CLI（官方）
    "claude": CLIProvider(
        name="Claude Code",
        cmd="claude",
        args_template=["-p", "{prompt}", "--output-format", "text"],
        timeout=180,
    ),
    # Claude Code via Node.js（Windows 兼容）
    "claude-node": CLIProvider(
        name="Claude Code (Node)",
        cmd="node",
        args_template=[
            "{node_modules}/@anthropic-ai/claude-code/cli.js",
            "-p", "{prompt}", "--output-format", "text"
        ],
        timeout=180,
    ),
    # OpenAI Codex CLI
    "codex": CLIProvider(
        name="OpenAI Codex",
        cmd="codex",
        args_template=["-p", "{prompt}"],
        timeout=180,
    ),
    # Google Gemini CLI（如果有）
    "gemini": CLIProvider(
        name="Google Gemini CLI",
        cmd="gemini",
        args_template=["-p", "{prompt}"],
        timeout=180,
    ),
    # 腾讯云 Cloud CLI
    "cloud": CLIProvider(
        name="Tencent Cloud CLI",
        cmd="cloud",
        args_template=["-p", "{prompt}"],
        timeout=180,
    ),
}

# API Providers（API 接口方式）
API_PROVIDERS: dict[str, APIProvider] = {
    # OpenAI GPT 系列
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
    # Claude via API
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
    # 腾讯云混元大模型
    "hunyuan": APIProvider(
        name="腾讯云混元",
        provider_type="openai",  # 混元兼容 OpenAI 格式
        model="hunyuan",
        base_url="https://hunyuan.cloud.tencent.com",
        max_tokens=4096,
        api_env_vars=("HUNYUAN_API_KEY",),
    ),
    # 智谱 GLM
    "zhipu-glm4": APIProvider(
        name="智谱 GLM-4",
        provider_type="openai",
        model="glm-4",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        max_tokens=4096,
        api_env_vars=("ZHIPU_API_KEY",),
    ),
    # 百度文心一言
    "wenxin": APIProvider(
        name="百度文心一言",
        provider_type="openai",
        model="ernie-4.0-8k-latest",
        base_url="https://qianfan.baidubce.com/v2",
        max_tokens=4096,
        api_env_vars=("ERNIE_API_KEY",),
    ),
    # 阿里通义千问
    "qwen": APIProvider(
        name="阿里通义千问",
        provider_type="openai",
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_tokens=4096,
        api_env_vars=("DASHSCOPE_API_KEY",),
    ),
    # DeepSeek
    "deepseek": APIProvider(
        name="DeepSeek",
        provider_type="openai",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        max_tokens=4096,
        api_env_vars=("DEEPSEEK_API_KEY",),
    ),
    # 本地 Ollama
    "ollama": APIProvider(
        name="Ollama (本地)",
        provider_type="openai",
        model="llama3",
        base_url="http://localhost:11434/v1",
        max_tokens=4096,
        api_env_vars=(),
    ),
    # Groq（免费高配额）
    "groq": APIProvider(
        name="Groq",
        provider_type="openai",
        model="llama-3.1-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        max_tokens=4096,
        api_env_vars=("GROQ_API_KEY",),
    ),
}


CLI_COMMAND_ENV_VARS: dict[str, str] = {
    "codex": "CODEPILOT_CODEX_CMD",
    "claude": "CODEPILOT_CLAUDE_CMD",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 配置（决定 Builder 和 Reviewer 用什么）
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentConfig:
    """Agent 运行配置."""
    builder: str = "codex"  # CLI name 或 API provider key
    reviewer: str = "codex"
    mode: str = "dual"  # "cli" | "api" | "dual"
    # API 密钥（可以从环境变量覆盖）
    api_keys: dict[str, str] = field(default_factory=dict)

    def resolve_builder(self) -> tuple[str, str]:
        """解析 builder，返回 (type, name)."""
        if self.builder in API_PROVIDERS:
            return "api", self.builder
        return "cli", self.builder

    def resolve_reviewer(self) -> tuple[str, str]:
        """解析 reviewer，返回 (type, name)."""
        if self.reviewer in API_PROVIDERS:
            return "api", self.reviewer
        return "cli", self.reviewer


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════════════════════════

TASK_PROMPT_TEMPLATE = """\
你是一个智能体任务规划助手。你的职责是根据用户提供的任务标题，生成一份结构化的任务描述文档。

**输出要求**：直接输出 Markdown 内容，不需要任何开场白、解释、或"以下是..."之类的废话。

**必须包含以下 6 个 section**（每个都要有实质内容）：

### 任务目标
清晰描述这个任务要做什么：输入、过程、输出各是什么。

### 验收标准
至少 3 条具体可验证的标准。能用命令验证的写命令，能直接看文件判断的写文件路径。

### Builder 职责（针对代码生成）
具体列出：需要读哪些文件、创建/修改哪些文件、怎样实现。

### Reviewer 职责（针对代码审查）
具体列出：需要 review 哪些内容、验证哪些方面、用什么标准判断通过。

### 涉及文件
列出任务预计修改的文件路径（相对于项目根目录）。只列真实可能的路径。

### 备注
前置条件、风险、注意事项（1-3 条即可）。

---
任务标题: {title}
{project_context}
"""


TASK_BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "complexity": {"type": "string", "enum": ["simple", "complex"]},
        "should_split": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "goal": {"type": "string"},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                    },
                    "builder_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reviewer_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "depends_on_indices": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                        "description": "依赖哪些子任务，按 tasks 数组下标；留空表示可以和其它无依赖任务并行",
                    },
                },
                "required": [
                    "title",
                    "priority",
                    "goal",
                    "acceptance_criteria",
                    "builder_notes",
                    "reviewer_notes",
                    "files",
                    "notes",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "complexity", "should_split", "tasks"],
    "additionalProperties": False,
}


TASK_BREAKDOWN_PROMPT_TEMPLATE = """\
你是一个资深技术负责人，负责把一个高层目标拆成可以自动执行的工程任务。

要求：
1. 先判断需求是 simple 还是 complex。
2. simple: 输出 1 个任务，should_split=false。
3. complex: 输出 2 到 {max_tasks} 个子任务，should_split=true，默认按线性顺序执行。
4. 每个任务都要足够具体，能直接交给代码代理执行。
5. 优先拆出“先修基础设施，再做能力”的顺序。
6. 只输出符合 schema 的 JSON，不要输出 Markdown，不要解释。
7. files 只写真实可能涉及的相对路径；不确定就少写，不要乱写。
8. acceptance_criteria、builder_notes、reviewer_notes、notes 都要有实际内容。

高层目标：
{title}

{project_context}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 项目上下文收集
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_project_context(project_path: str) -> str:
    """收集项目文件列表作为上下文."""
    if not project_path:
        return ""

    proj = Path(project_path)
    if not proj.exists():
        return ""

    parts = ["项目上下文（供参考）："]

    # Python 文件
    py_files = [
        str(p.relative_to(proj))
        for p in proj.rglob("*.py")
        if "__pycache__" not in str(p) and "venv" not in str(p)
           and ".venv" not in str(p) and "env" not in p.parent.name
    ]
    if py_files:
        parts.append(f"Python 文件: {', '.join(py_files[:20])}")

    # JS/TS 文件
    js_files = [
        str(p.relative_to(proj))
        for p in list(proj.rglob("*.js")) + list(proj.rglob("*.ts"))
        if "node_modules" not in str(p)
    ]
    if js_files:
        parts.append(f"JS/TS 文件: {', '.join(js_files[:15])}")

    # 文档
    md_files = [str(p.relative_to(proj)) for p in proj.rglob("*.md")]
    if md_files:
        parts.append(f"文档: {', '.join(md_files[:5])}")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Shell 检测（跨平台）
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_shell() -> str:
    """检测当前可用的 shell."""
    system = platform.system().lower()

    if system == "windows":
        # Windows: 优先 PowerShell 7，然后 PowerShell 5，最后 cmd
        for shell in ["pwsh", "powershell.exe", "cmd.exe"]:
            if shutil.which(shell):
                return shell
        return "powershell.exe"
    else:
        # Unix-like: 优先 zsh，然后 bash
        for shell in ["zsh", "bash", "sh"]:
            if shutil.which(shell):
                return shell
        return "bash"


def _get_node_modules_path() -> str:
    """获取全局 node_modules 路径."""
    node = shutil.which("node")
    if not node:
        return ""
    return str(Path(node).parent / "node_modules")


def _load_project_config(project_path: str | Path | None = None):
    """Load AGENTS.toml from an explicit project path or the current working tree."""
    return load_project_config(project_path)


def _configured_cli_command(provider_key: str, project_path: str | Path | None = None) -> str:
    """Resolve CLI command overrides from env vars or AGENTS.toml."""
    env_var = CLI_COMMAND_ENV_VARS.get(provider_key)
    if env_var:
        env_value = os.environ.get(env_var, "").strip()
        if env_value:
            return env_value

    cfg = _load_project_config(project_path)
    if not cfg:
        return ""
    if provider_key == "codex":
        return (cfg.codex_cmd or "").strip()
    if provider_key == "claude":
        return (cfg.claude_cmd or "").strip()
    return ""


def resolve_cli_provider(provider_key: str, project_path: str | Path | None = None) -> CLIProvider:
    """Return the effective CLI provider with project-local command overrides applied."""
    provider = CLI_PROVIDERS[provider_key]
    config_key = "claude" if provider_key == "claude" else provider_key
    override = _configured_cli_command(config_key, project_path)
    if override and override != provider.cmd:
        return replace(provider, cmd=override)
    return provider


# ═══════════════════════════════════════════════════════════════════════════════
# CLI 执行器
# ═══════════════════════════════════════════════════════════════════════════════

def _run_cli_provider(
    provider: CLIProvider,
    prompt: str,
    env_overrides: Optional[dict] = None,
) -> str:
    """执行 CLI Provider."""
    exe = provider.find_executable()
    if not exe:
        raise RuntimeError(f"未找到可执行文件: {provider.cmd}")

    # 替换 prompt
    args = []
    for arg in provider.args_template:
        arg = arg.replace("{prompt}", prompt)
        arg = arg.replace("{node_modules}", _get_node_modules_path())
        args.append(arg)

    # 构建命令
    cmd = [str(exe)] + args

    # 环境变量
    env = os.environ.copy()
    if provider.env_prepend:
        env.update(provider.env_prepend)
    if env_overrides:
        env.update(env_overrides)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=provider.timeout,
            env=env,
        )

        if result.returncode != 0:
            err = (result.stderr or "").strip() or "(无 stderr)"
            raise RuntimeError(
                f"{provider.name} CLI 失败 (退出码 {result.returncode}):\n{err}"
            )

        output = result.stdout.strip()
        if not output:
            raise RuntimeError(f"{provider.name} 返回了空内容")

        return output

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{provider.name} 执行超时（{provider.timeout}s）")


# ═══════════════════════════════════════════════════════════════════════════════
# API 执行器
# ═══════════════════════════════════════════════════════════════════════════════

def _run_api_provider(
    provider: APIProvider,
    prompt: str,
    system_prompt: Optional[str] = None,
) -> str:
    """执行 API Provider."""
    client, endpoint = provider.build_client()

    # 构建消息
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    try:
        if endpoint == "chat.completions":
            # OpenAI 兼容格式
            response = client.chat.completions.create(
                model=provider.model,
                messages=messages,
                max_tokens=provider.max_tokens,
                temperature=provider.temperature,
            )
            return response.choices[0].message.content.strip()

        elif endpoint == "messages":
            # Anthropic 格式
            response = client.messages.create(
                model=provider.model,
                max_tokens=provider.max_tokens,
                temperature=provider.temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()

        else:
            raise ValueError(f"未知端点类型: {endpoint}")

    except Exception as e:
        raise RuntimeError(f"{provider.name} API 调用失败: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 主入口函数
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_agent_name(name: str) -> str:
    """规范化 agent 名称，尝试找到匹配的 provider."""
    name = name.lower().strip()
    if name == "dual":
        return "dual"

    # 直接匹配
    if name in CLI_PROVIDERS or name in API_PROVIDERS:
        return name

    # 别名映射
    aliases = {
        "gpt4": "openai-gpt4",
        "gpt-4": "openai-gpt4",
        "gpt4o": "openai-gpt4o",
        "gpt-4o": "openai-gpt4o",
        "gpt35": "openai-gpt35",
        "gpt-3.5": "openai-gpt35",
        "sonnet": "claude-sonnet",
        "claude-sonnet": "claude-sonnet",
        "opus": "claude-opus",
        "claude-opus": "claude-opus",
        "haiku": "claude-haiku",
        "claude-haiku": "claude-haiku",
        "混元": "hunyuan",
        "hunyuan": "hunyuan",
        "glm": "zhipu-glm4",
        "glm4": "zhipu-glm4",
        "zhipu": "zhipu-glm4",
        "文心": "wenxin",
        "ernie": "wenxin",
        "通义": "qwen",
        "qwen": "qwen",
        "deepseek": "deepseek",
        "ollama": "ollama",
        "groq": "groq",
    }

    # 前缀匹配
    for key, value in aliases.items():
        if name.startswith(key):
            return value

    # 模糊匹配
    for cli_key in CLI_PROVIDERS:
        if cli_key in name or name in cli_key:
            return cli_key

    for api_key in API_PROVIDERS:
        if api_key.replace("-", "") in name.replace("-", "").replace("_", ""):
            return api_key

    return name


def generate_task_content(
    title: str,
    project_path: str = "",
    agent: str = "codex",
    api_keys: Optional[dict[str, str]] = None,
) -> str:
    """
    调用 AI 生成任务内容。

    支持的 agent 名称：
    - CLI: claude, codex, gemini, cloud
    - API: openai-gpt4, openai-gpt4o, claude-sonnet, claude-opus, hunyuan, qwen, deepseek, ollama 等

    Args:
        title: 任务标题
        project_path: 项目路径（提供上下文）
        agent: AI 模式（CLI 名称或 API provider key）
        api_keys: API 密钥 dict，格式 {"provider_name": "key"}

    Returns:
        生成的 Markdown 内容
    """
    # 规范化 agent 名称
    normalized = normalize_agent_name(agent)
    available, message = check_provider_availability(normalized, project_path=project_path)
    if not available:
        raise RuntimeError(message)
    if normalized == "dual":
        # 双代理模式用于执行阶段；生成任务内容时统一交给 Codex。
        normalized = "codex"

    # 收集上下文
    ctx = _collect_project_context(project_path)
    prompt = TASK_PROMPT_TEMPLATE.format(title=title, project_context=ctx)

    # API 密钥环境变量覆盖
    env_overrides = {}
    if api_keys:
        for provider, key in api_keys.items():
            env_key = f"{provider.upper()}_API_KEY"
            env_overrides[env_key] = key

    # 根据类型选择执行方式
    if normalized in API_PROVIDERS:
        provider = API_PROVIDERS[normalized]

        # 覆盖 API 密钥
        if api_keys and normalized in api_keys:
            provider.api_key = api_keys[normalized]

        return _run_api_provider(provider, prompt)

    elif normalized in CLI_PROVIDERS:
        provider = resolve_cli_provider(normalized, project_path)

        # Claude Node 特殊处理
        if normalized == "claude-node":
            node_modules = _get_node_modules_path()
            if not Path(node_modules, "@anthropic-ai", "claude-code", "cli.js").exists():
                raise RuntimeError(
                    f"未找到 Claude Code CLI: {node_modules}/@anthropic-ai/claude-code/cli.js\n"
                    "请运行: npm install -g @anthropic-ai/claude-code"
                )

        return _run_cli_provider(provider, prompt, env_overrides)

    else:
        raise ValueError(
            f"未知的 agent: {agent} (normalized: {normalized})\n"
            f"支持的 CLI: {', '.join(CLI_PROVIDERS.keys())}\n"
            f"支持的 API: {', '.join(API_PROVIDERS.keys())}"
        )


def list_available_providers() -> dict[str, list[str]]:
    """列出所有可用的 Providers."""
    cli_available = []
    for key, provider in CLI_PROVIDERS.items():
        if provider.find_executable():
            cli_available.append(f"{key} ({provider.name})")
        else:
            cli_available.append(f"{key} ({provider.name}) - 未安装")

    api_available = list(API_PROVIDERS.keys())

    return {
        "cli": cli_available,
        "api": api_available,
    }


def check_provider_availability(agent: str, project_path: str | Path | None = None) -> tuple[bool, str]:
    """
    检查 provider 是否可用。

    Returns:
        (is_available, message)
    """
    normalized = normalize_agent_name(agent)

    if normalized == "dual":
        codex_ok, codex_msg = check_provider_availability("codex", project_path=project_path)
        claude_ok, claude_msg = check_provider_availability("claude", project_path=project_path)
        if codex_ok and claude_ok:
            return True, "可用: dual 模式将使用 codex 负责实现、claude 负责审查"
        missing = []
        if not codex_ok:
            missing.append(codex_msg)
        if not claude_ok:
            missing.append(claude_msg)
        return False, "当前无法使用 dual 模式：" + "；".join(missing)

    if normalized in CLI_PROVIDERS:
        provider = resolve_cli_provider(normalized, project_path)
        exe = provider.find_executable()
        if not exe:
            return False, f"当前无法使用 {provider.name}，因为本机没有找到 `{provider.cmd}` 命令。"

        if normalized == "claude-node":
            cli_js = Path(_get_node_modules_path()) / "@anthropic-ai" / "claude-code" / "cli.js"
            if not cli_js.exists():
                return False, (
                    "当前无法使用 Claude Code (Node)，因为没有找到全局安装的 "
                    "`@anthropic-ai/claude-code`。请先执行: npm install -g @anthropic-ai/claude-code"
                )

        return True, f"可用: {provider.name}"

    elif normalized in API_PROVIDERS:
        provider = API_PROVIDERS[normalized]
        if provider.provider_type == "openai" and not OPENAI_AVAILABLE:
            return False, f"当前无法使用 {provider.name}，因为本机没有安装 openai 依赖。"
        if provider.provider_type == "anthropic" and not ANTHROPIC_AVAILABLE:
            return False, f"当前无法使用 {provider.name}，因为本机没有安装 anthropic 依赖。"

        api_key = provider.resolve_api_key()
        if provider.requires_api_key() and not api_key:
            env_names = " / ".join(provider.api_env_vars) or "对应的 API Key 环境变量"
            return False, f"当前无法使用 {provider.name}，因为还没有配置 API Key。请先设置 {env_names}。"

        return True, f"可用: {provider.name}"

    else:
        return False, f"没有找到名为 `{agent}` 的智能体。请改用 codepilot providers 查看可用列表。"


def resolve_agent_with_fallback(
    agent: str,
    project_path: str | Path | None = None,
    default_mode: str = "codex",
) -> tuple[str, str | None]:
    """Check if *agent* is usable; if not, fall back to *default_mode*.

    Returns:
        (effective_agent, fallback_reason)  — *fallback_reason* is ``None``
        when no fallback was needed.
    """
    normalized = normalize_agent_name(agent)
    available, message = check_provider_availability(normalized, project_path=project_path)
    if available:
        return normalized, None

    # Determine a usable fallback ------------------------------------------
    fallback = normalize_agent_name(default_mode) if default_mode else "codex"
    if fallback == normalized:
        # The default itself is the failing agent; hard-fallback to codex CLI.
        fallback = "codex"

    fb_available, fb_msg = check_provider_availability(fallback, project_path=project_path)
    if not fb_available:
        # Last resort: try codex
        fallback = "codex"
        fb_available, fb_msg = check_provider_availability(fallback, project_path=project_path)
        if not fb_available:
            # Nothing works — let the caller decide how to handle it.
            return normalized, None

    reason = (
        f"请求的 agent '{agent}' 不可用（{message}），"
        f"已自动回退到 '{fallback}'"
    )
    return fallback, reason


_normalize_agent_name = normalize_agent_name


def _extract_error_hint(raw: str) -> str:
    """Condense stderr / exception text into one readable line."""
    text = (raw or "").strip()
    if not text:
        return ""

    def _from_payload(payload: dict) -> str:
        value = payload.get("result") or payload.get("message")
        if isinstance(payload.get("error"), dict):
            value = payload["error"].get("message") or value
        elif isinstance(payload.get("error"), str):
            value = payload.get("error") or value
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                extracted = _from_payload(payload)
                if extracted:
                    text = extracted
                    break
        else:
            text = line
            break

    text = text.replace("You've hit your limit", "当前账号额度已用完")
    text = text.replace("resets", "重置时间")
    text = text.replace("·", "，")
    return text[:220]


def _run_claude_schema_prompt(
    prompt: str,
    schema: dict,
    *,
    planner: str = "codex",
    project_path: str = "",
    config_ref: str | Path | None = None,
    timeout: int = 240,
) -> dict:
    """Use Claude CLI to produce schema-constrained JSON output."""
    normalized = normalize_agent_name(planner)
    model_alias = None
    provider_key = normalized
    if normalized in {"claude-sonnet", "claude-opus", "claude-haiku"}:
        provider_key = "claude"
        model_alias = normalized.split("-", 1)[1]
    elif normalized not in {"claude", "claude-node"}:
        raise RuntimeError("当前自动拆分只支持 Claude 或 Codex 作为规划器。")

    provider_ref = config_ref or project_path
    provider = resolve_cli_provider(provider_key, provider_ref)
    exe = provider.find_executable()
    if not exe:
        raise RuntimeError(f"当前无法使用 {provider.name} 进行任务拆分。请先安装对应 CLI，或改用 codex。")

    cmd = [str(exe)]
    if provider_key == "claude-node":
        cli_js = Path(_get_node_modules_path()) / "@anthropic-ai" / "claude-code" / "cli.js"
        if not cli_js.exists():
            raise RuntimeError(
                "当前无法使用 Claude Code (Node) 进行任务拆分，因为没有找到全局安装的 "
                "`@anthropic-ai/claude-code`。请先执行: npm install -g @anthropic-ai/claude-code"
            )
        cmd.append(str(cli_js))

    cmd.extend([
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--permission-mode", "plan",
    ])
    if model_alias:
        cmd.extend(["--model", model_alias])
    # -p "prompt" 必须放最后，否则 claude CLI 会忽略 --json-schema
    cmd.extend(["-p", prompt])

    try:
        import threading, sys

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # stderr 线程实时打印 claude 进度
        def _stream_stderr():
            assert process.stderr is not None
            for line in process.stderr:
                stripped = line.rstrip()
                if stripped:
                    sys.stderr.write(f"  [planner] {stripped}\n")
                    sys.stderr.flush()

        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stderr_thread.start()

        # stdout 单独读（不用 communicate 避免和 stderr 线程冲突）
        stdout_chunks = []

        def _read_stdout():
            assert process.stdout is not None
            stdout_chunks.append(process.stdout.read())

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stdout_thread.start()

        # 等待进程结束
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=2)

        result_stdout = "".join(stdout_chunks)
        result_returncode = process.returncode
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{provider.name} 在任务拆分阶段超时了，{timeout} 秒内没有返回结果。"
            "可以稍后重试，或改用 codex 作为规划器。"
        ) from exc

    if result_returncode != 0:
        hint = _extract_error_hint(result_stdout)
        suffix = f"原因：{hint}" if hint else "请检查 Claude CLI 当前是否可用。"
        raise RuntimeError(f"{provider.name} 没有成功完成任务拆分。{suffix}")

    output = result_stdout.strip()
    if not output:
        raise RuntimeError("Claude 任务拆分返回空内容")

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{provider.name} 返回的任务拆分结果不是有效 JSON，暂时无法继续自动规划。"
        ) from exc

    if isinstance(payload, dict):
        if isinstance(payload.get("structured_output"), dict):
            return payload["structured_output"]
        if isinstance(payload.get("result"), dict):
            return payload["result"]
        return payload

    raise RuntimeError(f"{provider.name} 返回的任务拆分结果格式不正确，暂时无法继续自动规划。")


def _run_codex_schema_prompt(
    prompt: str,
    schema: dict,
    *,
    project_path: str = "",
    config_ref: str | Path | None = None,
    timeout: int = 240,
) -> dict:
    """Use Codex CLI with a JSON schema output contract."""
    provider_ref = config_ref or project_path
    available, message = check_provider_availability("codex", project_path=provider_ref)
    if not available:
        raise RuntimeError(message)
    exe = resolve_cli_provider("codex", provider_ref).find_executable()
    if not exe:
        raise RuntimeError("当前无法使用 Codex 进行任务拆分，因为本机没有找到 `codex` 命令。")

    import tempfile

    with tempfile.TemporaryDirectory(prefix="codepilot-plan-") as temp_dir:
        temp = Path(temp_dir)
        schema_path = temp / "schema.json"
        output_path = temp / "result.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")

        cmd = [str(exe)]
        if project_path:
            cmd.extend(["-C", project_path])
        cmd.extend(
            [
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--dangerously-bypass-approvals-and-sandbox",
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                "-",
            ]
        )

        try:
            import threading as _threading

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if process.stdin:
                process.stdin.write(prompt)
                process.stdin.close()

            def _stream_codex_stderr():
                assert process.stderr is not None
                for line in process.stderr:
                    stripped = line.rstrip()
                    if stripped:
                        import sys
                        sys.stderr.write(f"  [planner] {stripped}\n")
                        sys.stderr.flush()

            st = _threading.Thread(target=_stream_codex_stderr, daemon=True)
            st.start()

            try:
                stdout_data, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            st.join(timeout=2)
            codex_returncode = process.returncode
            codex_stdout = stdout_data or ""
            codex_stderr = ""
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Codex 在任务拆分阶段超时了，{timeout} 秒内没有返回结果。"
                "可以稍后重试，或改用 claude 作为规划器。"
            ) from exc

        if codex_returncode != 0:
            hint = _extract_error_hint(codex_stderr or codex_stdout)
            suffix = f"原因：{hint}" if hint else "请检查 Codex CLI 当前是否可用。"
            raise RuntimeError(f"Codex 没有成功完成任务拆分。{suffix}")

        output = output_path.read_text(encoding="utf-8", errors="replace").strip() if output_path.exists() else ""
        if not output:
            output = codex_stdout.strip()
        if not output:
            raise RuntimeError("Codex 没有返回任务拆分结果，暂时无法继续自动规划。")

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex 返回的任务拆分结果不是有效 JSON，暂时无法继续自动规划。") from exc

    if isinstance(payload, dict):
        return payload

    raise RuntimeError("Codex 返回的任务拆分结果格式不正确，暂时无法继续自动规划。")


def build_task_markdown_from_plan(task: dict) -> str:
    """Convert a structured task plan item into task markdown."""
    acceptance = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    builder_notes = "\n".join(f"- {item}" for item in task.get("builder_notes", []))
    reviewer_notes = "\n".join(f"- {item}" for item in task.get("reviewer_notes", []))
    files = "\n".join(f"- {item}" for item in task.get("files", [])) or "- （待确认）"
    notes = "\n".join(f"- {item}" for item in task.get("notes", [])) or "- 无"

    return "\n".join(
        [
            f"# {task['title']}",
            "",
            "## 任务目标",
            "",
            task.get("goal", "").strip(),
            "",
            "## 验收标准",
            "",
            acceptance or "- 待补充",
            "",
            "## Builder 职责",
            "",
            builder_notes or "- 待补充",
            "",
            "## Reviewer 职责",
            "",
            reviewer_notes or "- 待补充",
            "",
            "## 涉及文件",
            "",
            files,
            "",
            "## 备注",
            "",
            notes,
        ]
    )


def generate_task_breakdown(
    title: str,
    project_path: str = "",
    planner: str = "codex",
    max_tasks: int = 5,
    config_ref: str | Path | None = None,
) -> dict:
    """Generate a structured subtask breakdown for a high-level goal."""
    max_tasks = max(1, min(max_tasks, 8))
    normalized = normalize_agent_name(planner)
    context = _collect_project_context(project_path)
    prompt = TASK_BREAKDOWN_PROMPT_TEMPLATE.format(
        title=title,
        project_context=context,
        max_tasks=max_tasks,
    )

    if normalized in {"claude", "claude-node", "claude-sonnet", "claude-opus", "claude-haiku"}:
        breakdown = _run_claude_schema_prompt(
            prompt,
            TASK_BREAKDOWN_SCHEMA,
            planner=normalized,
            project_path=project_path,
            config_ref=config_ref,
        )
    elif normalized == "codex":
        breakdown = _run_codex_schema_prompt(
            prompt,
            TASK_BREAKDOWN_SCHEMA,
            project_path=project_path,
            config_ref=config_ref,
        )
    else:
        raise RuntimeError(
            f"当前自动拆分暂时不支持规划器 `{planner}`。请改用 claude 或 codex。"
        )
    tasks = breakdown.get("tasks") or []
    if not tasks:
        raise RuntimeError("任务拆分结果为空")
    breakdown["tasks"] = tasks[:max_tasks]
    breakdown.setdefault("complexity", "simple" if len(breakdown["tasks"]) <= 1 else "complex")
    breakdown.setdefault("should_split", len(breakdown["tasks"]) > 1)
    return breakdown


# ═══════════════════════════════════════════════════════════════════════════════
# 意图分类器
# ═══════════════════════════════════════════════════════════════════════════════

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["question", "task", "requirement", "command"],
        },
        "reason": {"type": "string"},
    },
    "required": ["intent"],
    "additionalProperties": False,
}

INTENT_PROMPT = """你是一个输入意图分类器。请把用户下面的一句话分到下列四类之一，并以 JSON 返回：

- question: 用户是在问问题、求解释或求建议，不需要你去改代码或建任务。
- task: 用户想做一件具体小事，一步就能完成，不需要拆分。
- requirement: 用户想做一个较大的需求，涉及多步或多模块，需要拆分成子任务。
- command: 用户想直接调 codepilot 自身的某个命令（查看状态、日志、重试、停止、巡检、发布等），不是对代码本身下需求。

只输出 JSON，字段：intent, reason（一句中文说明判断依据）。

用户输入：
{text}
"""


def _classify_via_api(
    provider: APIProvider,
    text: str,
    timeout: int = 30,
) -> dict:
    """Call an API provider with the intent classification prompt."""
    _ = timeout  # API clients have their own timeouts
    raw = _run_api_provider(provider, INTENT_PROMPT.format(text=text))
    # 宽松解析：可能带 ``` 或前缀
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start : end + 1]
    return json.loads(raw)


def _classify_via_codex(
    text: str,
    project_path: str = "",
    timeout: int = 30,
) -> dict:
    """Fallback: use local codex CLI with schema-constrained output."""
    return _run_codex_schema_prompt(
        INTENT_PROMPT.format(text=text),
        INTENT_SCHEMA,
        project_path=project_path,
        timeout=timeout,
    )


def _heuristic_intent(text: str) -> Optional[str]:
    """Cheap rule-based pre-filter. Returns None if unsure."""
    t = text.strip()
    if not t:
        return None
    # 直接命中 codepilot 内建命令动词
    command_keywords = (
        "查看状态", "看一下状态", "看看状态", "列出任务", "看看任务",
        "查看日志", "看日志", "重试任务", "停止任务", "跑一下巡检", "触发巡检",
        "发布", "打包", "构建二进制",
    )
    for kw in command_keywords:
        if kw in t:
            return "command"
    # 以问号结尾 → question
    if t.endswith("?") or t.endswith("？"):
        return "question"
    # 常见疑问词开头
    question_starts = (
        "怎么", "如何", "为什么", "为啥", "什么是", "什么叫",
        "能不能", "可不可以", "是不是", "有没有", "哪里", "哪个",
        "解释", "说明", "介绍", "告诉我", "请问",
    )
    for word in question_starts:
        if t.startswith(word):
            return "question"
    # 句中含疑问词（"做什么的"、"是什么"、"有哪些"、"怎样"、"吗"结尾等）
    question_contains = (
        "是什么", "做什么", "有什么", "有哪些", "哪些", "怎样", "怎么样",
        "能做什么", "提供什么", "支持什么", "包含什么",
        "是干什么", "干什么的", "干嘛的", "用来做什么",
        "多少", "几个", "啥意思", "什么意思",
    )
    for word in question_contains:
        if word in t:
            return "question"
    if t.endswith("吗") or t.endswith("呢") or t.endswith("吧？"):
        return "question"
    # 含"帮我""实现""修复""添加""优化"等动词 → requirement
    requirement_verbs = (
        "帮我", "实现", "修复", "修改", "添加", "新增", "优化", "重构",
        "删除", "移除", "升级", "迁移", "部署", "接入",
    )
    for word in requirement_verbs:
        if word in t:
            return "requirement"
    return None


def classify_intent(
    text: str,
    project_path: str = "",
    classifier_provider: str = "",
    classifier_model: str = "",
    timeout: int = 30,
    api_key: Optional[str] = None,
) -> dict:
    """Classify a chat input as question / task / requirement.

    Strategy:
      1. Heuristic pre-filter (free, instant).
      2. Configured API provider if available.
      3. Local codex CLI fallback.
      4. On any failure, default to 'requirement' (preserves current behavior).
    """
    text = text.strip()
    if not text:
        return {"intent": "requirement", "reason": "空输入", "source": "default"}

    guess = _heuristic_intent(text)
    if guess:
        return {"intent": guess, "reason": "启发式规则命中", "source": "heuristic"}

    # API path
    valid_intents = {"question", "task", "requirement", "command"}
    if classifier_provider and classifier_provider in API_PROVIDERS:
        provider = replace(API_PROVIDERS[classifier_provider])
        if classifier_model:
            provider.model = classifier_model
        if api_key:
            provider.api_key = api_key
        try:
            if not provider.requires_api_key() or provider.resolve_api_key():
                payload = _classify_via_api(provider, text, timeout=timeout)
                intent = payload.get("intent")
                if intent in valid_intents:
                    return {
                        "intent": intent,
                        "reason": payload.get("reason", ""),
                        "source": f"api:{classifier_provider}",
                    }
        except Exception as exc:
            # API 失败 → 继续尝试本地
            last_error = str(exc)
        else:
            last_error = ""
    else:
        last_error = ""

    # Local codex fallback
    try:
        payload = _classify_via_codex(text, project_path=project_path, timeout=timeout)
        intent = payload.get("intent")
        if intent in valid_intents:
            return {
                "intent": intent,
                "reason": payload.get("reason", ""),
                "source": "codex",
            }
    except Exception as exc:
        last_error = str(exc)

    return {
        "intent": "requirement",
        "reason": f"分类失败，默认当作需求处理（{last_error or '未知原因'}）",
        "source": "default",
    }


def answer_question_via_api(
    provider_key: str,
    question: str,
    project_path: str = "",
    model_override: str = "",
    api_key: Optional[str] = None,
) -> str:
    """Answer a user question directly without creating a task."""
    context = _collect_project_context(project_path)
    prompt = (
        "你是当前项目的协作助手。请基于下面的项目上下文，"
        "用简洁中文直接回答用户的问题。如果不确定，明确说不确定。\n\n"
        f"## 项目上下文\n{context}\n\n## 用户问题\n{question}"
    )
    if provider_key and provider_key in API_PROVIDERS:
        provider = replace(API_PROVIDERS[provider_key])
        if model_override:
            provider.model = model_override
        if api_key:
            provider.api_key = api_key
        return _run_api_provider(provider, prompt)
    # 无 API 时用本地 claude CLI 回答
    return _answer_via_local_cli(prompt, project_path=project_path)


def _answer_via_local_cli(prompt: str, project_path: str = "", timeout: int = 120) -> str:
    """Use claude or codex CLI to answer a question directly."""
    # 优先 claude
    for cli_name in ("claude", "codex"):
        try:
            provider = resolve_cli_provider(cli_name, project_path or None)
            exe = provider.find_executable()
        except Exception:
            continue
        if not exe:
            continue

        cmd = [str(exe), "-p", "--output-format", "text"]
        if cli_name == "codex":
            cmd = [str(exe), "exec", "--skip-git-repo-check", "--ephemeral",
                   "--dangerously-bypass-approvals-and-sandbox"]
            if project_path:
                cmd = [str(exe), "-C", project_path] + cmd[1:]

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            if result.returncode == 0 and (result.stdout or "").strip():
                return result.stdout.strip()
        except Exception:
            continue
    return ""
