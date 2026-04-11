"""AI 任务内容生成模块。支持 CLI 和 API 两种调用方式。"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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

    def build_client(self):
        """构建 API 客户端."""
        if self.provider_type == "openai":
            if not OPENAI_AVAILABLE:
                raise RuntimeError("请安装 openai: pip install openai")
            client = openai.OpenAI(
                api_key=self.api_key or os.environ.get("OPENAI_API_KEY", ""),
                base_url=self.base_url or None,
            )
            return client, "chat.completions"
        elif self.provider_type == "anthropic":
            if not ANTHROPIC_AVAILABLE:
                raise RuntimeError("请安装 anthropic: pip install anthropic")
            client = anthropic.Anthropic(
                api_key=self.api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
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
    ),
    "openai-gpt4o": APIProvider(
        name="OpenAI GPT-4o",
        provider_type="openai",
        model="gpt-4o",
        max_tokens=4096,
    ),
    "openai-gpt35": APIProvider(
        name="OpenAI GPT-3.5 Turbo",
        provider_type="openai",
        model="gpt-3.5-turbo",
        max_tokens=2048,
    ),
    # Claude via API
    "claude-opus": APIProvider(
        name="Claude 3 Opus",
        provider_type="anthropic",
        model="claude-opus-4-20241120",
        max_tokens=4096,
    ),
    "claude-sonnet": APIProvider(
        name="Claude 3.5 Sonnet",
        provider_type="anthropic",
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
    ),
    "claude-haiku": APIProvider(
        name="Claude 3 Haiku",
        provider_type="anthropic",
        model="claude-3-5-haiku-20240307",
        max_tokens=2048,
    ),
    # 腾讯云混元大模型
    "hunyuan": APIProvider(
        name="腾讯云混元",
        provider_type="openai",  # 混元兼容 OpenAI 格式
        model="hunyuan",
        base_url="https://hunyuan.cloud.tencent.com",
        max_tokens=4096,
    ),
    # 智谱 GLM
    "zhipu-glm4": APIProvider(
        name="智谱 GLM-4",
        provider_type="openai",
        model="glm-4",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        max_tokens=4096,
    ),
    # 百度文心一言
    "wenxin": APIProvider(
        name="百度文心一言",
        provider_type="openai",
        model="ernie-4.0-8k-latest",
        base_url="https://qianfan.baidubce.com/v2",
        max_tokens=4096,
    ),
    # 阿里通义千问
    "qwen": APIProvider(
        name="阿里通义千问",
        provider_type="openai",
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_tokens=4096,
    ),
    # DeepSeek
    "deepseek": APIProvider(
        name="DeepSeek",
        provider_type="openai",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        max_tokens=4096,
    ),
    # 本地 Ollama
    "ollama": APIProvider(
        name="Ollama (本地)",
        provider_type="openai",
        model="llama3",
        base_url="http://localhost:11434/v1",
        max_tokens=4096,
    ),
    # Groq（免费高配额）
    "groq": APIProvider(
        name="Groq",
        provider_type="openai",
        model="llama-3.1-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        max_tokens=4096,
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 配置（决定 Builder 和 Reviewer 用什么）
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentConfig:
    """Agent 运行配置."""
    builder: str = "codex"  # CLI name 或 API provider key
    reviewer: str = "claude"
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

def _normalize_agent_name(name: str) -> str:
    """规范化 agent 名称，尝试找到匹配的 provider."""
    name = name.lower().strip()

    # 直接匹配
    if name in CLI_PROVIDERS or name in API_PROVIDERS:
        return name

    # 别名映射
    aliases = {
        "dual": "claude",  # 默认用 claude
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

    # 默认返回 claude
    return "claude"


def generate_task_content(
    title: str,
    project_path: str = "",
    agent: str = "claude",
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
    normalized = _normalize_agent_name(agent)

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
        provider = CLI_PROVIDERS[normalized]

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


def check_provider_availability(agent: str) -> tuple[bool, str]:
    """
    检查 provider 是否可用。

    Returns:
        (is_available, message)
    """
    normalized = _normalize_agent_name(agent)

    if normalized in CLI_PROVIDERS:
        provider = CLI_PROVIDERS[normalized]
        exe = provider.find_executable()
        if exe:
            return True, f"可用: {exe}"
        else:
            return False, f"未找到可执行文件: {provider.cmd}"

    elif normalized in API_PROVIDERS:
        provider = API_PROVIDERS[normalized]
        api_key = provider.api_key or os.environ.get(
            f"{provider.provider_type.upper()}_API_KEY", ""
        )
        if api_key:
            return True, f"可用 (API Key 已配置)"
        else:
            return True, f"可用 (请设置 API Key 或环境变量)"

    else:
        return False, f"未知的 agent: {agent}"
