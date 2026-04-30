"""Provider dataclasses, registries, shell/env helpers and runners.

Split out from ai.py for maintainability. All symbols are re-exported by
`codepilot.ai` so existing `from codepilot.ai_support.service import CLIProvider` keeps working.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
import importlib.util
import json
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
import urllib.request

from codepilot.ai_support.planner_context import collect_planner_context
from codepilot.core.config import load_project_config
from codepilot.core.text_decode import decode_subprocess_text

# API support libs (optional and lazily imported).
# Keep availability flags cheap so CLI startup does not import heavy SDK trees.
OPENAI_AVAILABLE = importlib.util.find_spec("openai") is not None
ANTHROPIC_AVAILABLE = importlib.util.find_spec("anthropic") is not None


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
    usage_key: str = ""
    auto_model_selection: bool = False
    simple_model: str = ""
    complex_model: str = ""
    thinking: str = ""  # "", "auto", "enabled", "disabled"
    reasoning_effort: str = ""  # "", "auto", "high", "max"
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
            from openai import OpenAI

            client = OpenAI(
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
            from anthropic import Anthropic

            client = Anthropic(
                api_key=api_key,
                base_url=self.base_url or None,
            )
            return client, "messages"
        else:
            raise ValueError(f"不支持的 provider_type: {self.provider_type}")



# CLI Providers（命令行方式）
CLI_PROVIDERS: dict[str, CLIProvider] = {
    # Claude Code CLI（官方）
    "claude": CLIProvider(
        name="Claude Code",
        cmd="claude",
        args_template=["-p", "{prompt}", "--output-format", "text", "--dangerously-skip-permissions"],
        timeout=180,
    ),
    # Claude Code via Node.js（Windows 兼容）
    "claude-node": CLIProvider(
        name="Claude Code (Node)",
        cmd="node",
        args_template=[
            "{node_modules}/@anthropic-ai/claude-code/cli.js",
            "-p", "{prompt}", "--output-format", "text", "--dangerously-skip-permissions"
        ],
        timeout=180,
    ),
    # OpenAI Codex CLI
    "codex": CLIProvider(
        name="OpenAI Codex",
        cmd="codex",
        args_template=[
            "exec", "--ephemeral", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "{prompt}",
        ],
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


def resolve_api_provider(
    provider_key: str,
    project_path: str | Path | dict | None = None,
    *,
    config_file: str | Path | None = None,
) -> APIProvider:
    """Return an API provider with AGENTS.toml overrides applied."""
    if provider_key not in API_PROVIDERS:
        raise KeyError(provider_key)

    provider = replace(API_PROVIDERS[provider_key])
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
_PROJECT_MARKER_FILES = (
    "AGENTS.toml",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "requirements.txt",
)

_DISCOVERY_SKIP_DIRS = {
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "target",
    ".next",
    "AppData",
}

_HOME_LIKE_DIR_NAMES = {
    "desktop",
    "documents",
    "downloads",
    "music",
    "pictures",
    "videos",
    "favorites",
}


def _project_candidate_score(path: Path) -> int:
    """Score whether ``path`` looks like a project root."""
    if not path.exists() or not path.is_dir():
        return 0

    score = 0
    if (path / "AGENTS.toml").is_file():
        score += 8
    if (path / ".git").exists():
        score += 6

    marker_hits = sum(1 for name in _PROJECT_MARKER_FILES if (path / name).exists())
    if marker_hits:
        score += min(8, marker_hits * 2)

    has_readme = any((path / name).is_file() for name in ("README.md", "README", "README.txt"))
    if has_readme:
        score += 1
    if (path / "src").is_dir():
        score += 1
    return score


def _is_home_like_root(path: Path) -> bool:
    """Best-effort probe for user-home style directory rather than project root."""
    if not path.exists() or not path.is_dir():
        return False
    if _project_candidate_score(path) >= 5:
        return False

    names: set[str] = set()
    try:
        for child in path.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            names.add(child.name.lower())
    except Exception:
        return False

    return len(names & _HOME_LIKE_DIR_NAMES) >= 2


def _discover_project_root(base_path: Path, *, max_depth: int = 2, max_dirs: int = 80) -> Path:
    """Discover a likely project root from ``base_path`` in bounded breadth-first search."""
    if _project_candidate_score(base_path) >= 5:
        return base_path

    queue: deque[tuple[Path, int]] = deque([(base_path, 0)])
    seen: set[Path] = {base_path}
    candidates: list[tuple[int, int, Path]] = []
    scanned = 0

    while queue and scanned < max_dirs:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            children = sorted((p for p in current.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
        except Exception:
            continue
        for child in children:
            if scanned >= max_dirs:
                break
            if child in seen:
                continue
            seen.add(child)
            scanned += 1

            if child.name.startswith(".") or child.name in _DISCOVERY_SKIP_DIRS:
                continue
            if child.is_symlink():
                continue

            score = _project_candidate_score(child)
            if score >= 5:
                candidates.append((score, depth + 1, child))

            if depth + 1 < max_depth:
                queue.append((child, depth + 1))

    if not candidates:
        return base_path

    candidates.sort(key=lambda item: (-item[0], item[1], str(item[2]).lower()))
    best_score, _, best_path = candidates[0]
    close_alternatives = [item for item in candidates[1:] if item[0] >= best_score - 1]
    # Ambiguous candidates: keep current path to avoid random jumps.
    if close_alternatives and (best_score < 10 or len(close_alternatives) >= 2):
        return base_path
    return best_path


def _readme_snippet(path: Path, *, max_chars: int = 700) -> str:
    for name in ("README.md", "README", "README.txt"):
        candidate = path / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if not text:
            continue
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n…（已截断）"
        return f"{name} 摘录:\n{text}"
    return ""


def _lightweight_project_outline(path: Path) -> str:
    parts: list[str] = []
    try:
        top_items = sorted(
            p.name
            for p in path.iterdir()
            if not p.name.startswith(".") and p.name not in _DISCOVERY_SKIP_DIRS
        )
    except Exception:
        top_items = []

    if top_items:
        parts.append(f"顶层: {', '.join(top_items[:24])}")

    stack_hints: list[str] = []
    if (path / "pyproject.toml").exists():
        stack_hints.append("Python (pyproject.toml)")
    if (path / "package.json").exists():
        stack_hints.append("Node.js (package.json)")
    if (path / "Cargo.toml").exists():
        stack_hints.append("Rust (Cargo.toml)")
    if (path / "go.mod").exists():
        stack_hints.append("Go (go.mod)")
    if stack_hints:
        parts.append("技术栈: " + "、".join(stack_hints))

    readme = _readme_snippet(path)
    if readme:
        parts.append(readme)

    return "\n".join(parts)


def _collect_project_context(project_path: str, query_text: str = "") -> str:
    """Collect context for Q&A and auto-discover likely project root in temporary sessions."""
    if not project_path:
        return ""

    base = Path(project_path)
    if not base.exists():
        return ""

    resolved = _discover_project_root(base)
    parts: list[str] = []
    if resolved != base:
        parts.append(f"上下文目录: {resolved}（从 {base} 自动探测）")
    else:
        parts.append(f"上下文目录: {resolved}")

    if _is_home_like_root(base) and resolved == base:
        parts.append(
            "检测到当前目录更像用户主目录，不是明确项目根。"
            "回答时先说明这一点，并建议用户在目标目录运行 codepilot init 或使用 /project 切换。"
        )
        outline = _lightweight_project_outline(base)
        if outline:
            parts.append(outline)
        return "\n".join(parts)

    rich_context = collect_planner_context(str(resolved), requirement_title=query_text, max_chars=2600)
    if rich_context:
        parts.append(rich_context)
    else:
        outline = _lightweight_project_outline(resolved)
        if outline:
            parts.append(outline)

    return "\n".join(parts)




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


def _load_project_config(project_path: str | Path | dict | None = None):
    """Load AGENTS.toml from an explicit project path or the current working tree."""
    return load_project_config(project_path)


def _configured_cli_command(provider_key: str, project_path: str | Path | dict | None = None) -> str:
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


def resolve_cli_provider(provider_key: str, project_path: str | Path | dict | None = None) -> CLIProvider:
    """Return the effective CLI provider with project-local command overrides applied."""
    provider = CLI_PROVIDERS[provider_key]
    config_key = "claude" if provider_key == "claude" else provider_key
    override = _configured_cli_command(config_key, project_path)
    if override and override != provider.cmd:
        return replace(provider, cmd=override)
    return provider


def _ensure_claude_git_bash_env() -> None:
    """On Windows, auto-detect git-bash and set CLAUDE_CODE_GIT_BASH_PATH if missing."""
    if platform.system().lower() != "windows":
        return
    if os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
        return
    for candidate in [
        Path("D:/Program Files/Git/bin/bash.exe"),
        Path("C:/Program Files/Git/bin/bash.exe"),
        Path("C:/Program Files (x86)/Git/bin/bash.exe"),
    ]:
        if candidate.exists():
            os.environ["CLAUDE_CODE_GIT_BASH_PATH"] = str(candidate)
            return
    # Try to find via PATH
    bash = shutil.which("bash")
    if bash:
        os.environ["CLAUDE_CODE_GIT_BASH_PATH"] = bash


# Auto-run on import for Windows
_ensure_claude_git_bash_env()




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
            text=False,
            timeout=provider.timeout,
            env=env,
        )
        stdout_text = decode_subprocess_text(result.stdout).strip()
        stderr_text = decode_subprocess_text(result.stderr).strip()

        if result.returncode != 0:
            err = stderr_text or "(无 stderr)"
            raise RuntimeError(
                f"{provider.name} CLI 失败 (退出码 {result.returncode}):\n{err}"
            )

        output = stdout_text
        if not output:
            raise RuntimeError(f"{provider.name} 返回了空内容")

        return output

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{provider.name} 执行超时（{provider.timeout}s）")


@dataclass(frozen=True)
class _APIRunContext:
    provider: Any
    client: Any
    prompt: str
    system_prompt: Optional[str]
    messages: list[dict[str, str]]
    started_at: float
    model: str
    difficulty: str = "simple"
    thinking: str = ""
    reasoning_effort: str = ""


@dataclass(frozen=True)
class _APIRequestProfile:
    model: str
    difficulty: str
    thinking: str = ""
    reasoning_effort: str = ""


def _build_api_messages(prompt: str, system_prompt: Optional[str] = None) -> list[dict[str, str]]:
    """Build chat-style messages for OpenAI-compatible request payloads."""
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _should_stream_llm_progress() -> bool:
    try:
        from codepilot.core import progress_bus

        return progress_bus.has_subscribers()
    except Exception:
        return False


def _estimate_tokens(text: str) -> int:
    content = str(text or "")
    if not content:
        return 0
    return max(1, (len(content.encode("utf-8")) + 3) // 4)


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
    "classify",
)


def _classify_prompt_difficulty(prompt: str, system_prompt: Optional[str] = None) -> str:
    """Small deterministic heuristic for routing cost/quality-sensitive API calls."""
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


def _provider_usage_key(provider: Any) -> str:
    explicit = str(getattr(provider, "usage_key", "") or "").strip()
    if explicit:
        return explicit
    name = str(getattr(provider, "name", "") or "").strip().lower()
    return name.replace(" ", "-") or "unknown"


def mark_provider_unavailable(
    provider_key: str,
    provider: Any = None,
    reason: str = "",
    *,
    source: str = "",
    project_path: str = "",
) -> None:
    """Persist a lightweight marker when an API provider is skipped or fails."""
    scope = str(provider_key or "").strip() or _provider_usage_key(provider)
    if not scope:
        return
    try:
        from codepilot.storage import database as db

        db.init_db()
        existing = db.get_service_state("ai_provider", scope) or {}
        meta = existing.get("meta") if isinstance(existing.get("meta"), dict) else {}
        meta = dict(meta or {})
        meta.update(
            {
                "provider": scope,
                "name": str(getattr(provider, "name", "") or scope),
                "reason": str(reason or "provider unavailable"),
                "source": str(source or ""),
                "project_path": str(project_path or ""),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        db.upsert_service_state("ai_provider", scope, status="unavailable", meta=meta)
    except Exception:
        return


def _is_deepseek_provider(provider: Any) -> bool:
    base_url = str(getattr(provider, "base_url", "") or "").lower()
    return _provider_usage_key(provider) == "deepseek" or "api.deepseek.com" in base_url


def _build_api_request_profile(
    provider: Any,
    prompt: str,
    system_prompt: Optional[str] = None,
) -> _APIRequestProfile:
    base_model = str(getattr(provider, "model", "") or "").strip()
    difficulty = _classify_prompt_difficulty(prompt, system_prompt)
    model = base_model
    thinking = ""
    reasoning_effort = ""

    if _is_deepseek_provider(provider):
        simple_model = str(getattr(provider, "simple_model", "") or "").strip()
        complex_model = str(getattr(provider, "complex_model", "") or "").strip()
        if bool(getattr(provider, "auto_model_selection", False)):
            if not simple_model or not complex_model:
                raise RuntimeError(
                    f"{getattr(provider, 'name', 'provider')} 已开启自动模型切换，"
                    "但 simple_model / complex_model 未配置完整。"
                )
            model = complex_model if difficulty in {"hard", "xhard"} else simple_model
        else:
            model = base_model or simple_model or complex_model

        configured_thinking = str(getattr(provider, "thinking", "") or "auto").strip().lower()
        if configured_thinking in {"enabled", "disabled"}:
            thinking = configured_thinking
        elif configured_thinking == "auto":
            thinking = "enabled" if difficulty in {"hard", "xhard"} else "disabled"

        configured_effort = str(getattr(provider, "reasoning_effort", "") or "auto").strip().lower()
        if thinking == "enabled":
            if configured_effort in {"high", "max"}:
                reasoning_effort = configured_effort
            else:
                reasoning_effort = "max" if difficulty == "xhard" else "high"

    return _APIRequestProfile(
        model=model,
        difficulty=difficulty,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )


def _emit_llm_heartbeat(ctx: _APIRunContext, text: str, *, final: bool = False) -> None:
    try:
        from codepilot.core import progress_bus

        bus_ctx = progress_bus.current_llm_context()
        elapsed = round(max(0.0, time.monotonic() - ctx.started_at), 1)
        estimated_tokens = _estimate_tokens(text)
        label = str(bus_ctx.get("label") or ctx.provider.name)
        if final:
            message = f"{label} 完成：{elapsed}s，约 {estimated_tokens} tokens"
            level = "info"
            event_type = "phase_end"
        elif estimated_tokens <= 0:
            message = f"{label} 请求已发出：{elapsed}s，等待首个 token"
            level = "heartbeat"
            event_type = "phase_start"
        else:
            message = f"{label} 生成中：{elapsed}s，约 {estimated_tokens} tokens"
            level = "heartbeat"
            event_type = "heartbeat"
        progress_bus.emit(
            task_id=bus_ctx.get("task_id"),
            stage=str(bus_ctx.get("stage") or "system"),
            level=level,
            event_type=event_type,
            message=message,
            extra={
                "llm_heartbeat": True,
                "provider": ctx.provider.name,
                "model": ctx.model,
                "difficulty": ctx.difficulty,
                "thinking": ctx.thinking,
                "reasoning_effort": ctx.reasoning_effort,
                "elapsed_seconds": elapsed,
                "estimated_tokens": estimated_tokens,
                "text_chars": len(text or ""),
                "final": final,
            },
        )
    except Exception:
        pass


def _extract_openai_chunk_text(chunk: Any) -> str:
    pieces: list[str] = []
    for choice in getattr(chunk, "choices", []) or []:
        delta = getattr(choice, "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            pieces.append(content)
            continue
        if isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    pieces.append(item)
                    continue
                text = ""
                if isinstance(item, dict):
                    text = str(item.get("text") or "")
                else:
                    text = str(getattr(item, "text", "") or "")
                if text:
                    pieces.append(text)
    return "".join(pieces)


def _openai_request_kwargs(ctx: _APIRunContext, *, stream: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": ctx.model,
        "messages": ctx.messages,
        "max_tokens": ctx.provider.max_tokens,
        "temperature": ctx.provider.temperature,
    }
    if ctx.thinking:
        kwargs["thinking"] = {"type": ctx.thinking}
    if ctx.reasoning_effort:
        kwargs["reasoning_effort"] = ctx.reasoning_effort
    if stream:
        kwargs["stream"] = True
        if _is_deepseek_provider(ctx.provider):
            kwargs["stream_options"] = {"include_usage": True}
    return kwargs


def _usage_get(usage: Any, key: str, default: Any = 0) -> Any:
    if usage is None:
        return default
    if isinstance(usage, dict):
        return usage.get(key, default)
    return getattr(usage, key, default)


def _extract_token_usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    details = _usage_get(usage, "completion_tokens_details", {}) or {}
    values = {
        "prompt_tokens": _usage_get(usage, "prompt_tokens", 0),
        "completion_tokens": _usage_get(usage, "completion_tokens", 0),
        "total_tokens": _usage_get(usage, "total_tokens", 0),
        "prompt_cache_hit_tokens": _usage_get(usage, "prompt_cache_hit_tokens", 0),
        "prompt_cache_miss_tokens": _usage_get(usage, "prompt_cache_miss_tokens", 0),
        "reasoning_tokens": _usage_get(details, "reasoning_tokens", 0),
    }
    out: dict[str, int] = {}
    for key, value in values.items():
        try:
            out[key] = max(0, int(value or 0))
        except (TypeError, ValueError):
            out[key] = 0
    if not any(out.values()):
        return {}
    if out["total_tokens"] <= 0:
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


def _record_api_usage(ctx: _APIRunContext, usage: Any) -> None:
    values = _extract_token_usage(usage)
    if not values:
        return
    try:
        from codepilot.storage import database as db

        db.init_db()
        scope = _provider_usage_key(ctx.provider)
        existing = db.get_service_state("ai_usage", scope) or {}
        meta = existing.get("meta") if isinstance(existing.get("meta"), dict) else {}
        meta = dict(meta or {})
        meta["provider"] = scope
        meta["name"] = str(getattr(ctx.provider, "name", "") or scope)
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        meta["requests"] = int(meta.get("requests") or 0) + 1
        for key, value in values.items():
            meta[key] = int(meta.get(key) or 0) + int(value or 0)

        by_model = meta.get("by_model") if isinstance(meta.get("by_model"), dict) else {}
        model_stats = by_model.get(ctx.model) if isinstance(by_model.get(ctx.model), dict) else {}
        model_stats["requests"] = int(model_stats.get("requests") or 0) + 1
        model_stats["difficulty"] = ctx.difficulty
        model_stats["thinking"] = ctx.thinking
        model_stats["reasoning_effort"] = ctx.reasoning_effort
        for key, value in values.items():
            model_stats[key] = int(model_stats.get(key) or 0) + int(value or 0)
        by_model[ctx.model] = model_stats
        meta["by_model"] = by_model

        db.upsert_service_state("ai_usage", scope, status="active", meta=meta)
    except Exception:
        return


def _run_openai_sync(ctx: _APIRunContext) -> str:
    response = ctx.client.chat.completions.create(**_openai_request_kwargs(ctx))
    _record_api_usage(ctx, getattr(response, "usage", None))
    return (response.choices[0].message.content or "").strip()


def _run_openai_stream(ctx: _APIRunContext) -> str:
    parts: list[str] = []
    last_emit_at = 0.0
    _emit_llm_heartbeat(ctx, "", final=False)
    stream_usage = None
    stream = ctx.client.chat.completions.create(**_openai_request_kwargs(ctx, stream=True))
    for chunk in stream:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            stream_usage = usage
        piece = _extract_openai_chunk_text(chunk)
        if piece:
            parts.append(piece)
        now = time.monotonic()
        if piece and (last_emit_at <= 0 or now - last_emit_at >= 1.0):
            _emit_llm_heartbeat(ctx, "".join(parts), final=False)
            last_emit_at = now
    output = "".join(parts).strip()
    _record_api_usage(ctx, stream_usage)
    if output:
        _emit_llm_heartbeat(ctx, output, final=True)
    return output


def _anthropic_request_kwargs(ctx: _APIRunContext, *, stream: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": ctx.provider.model,
        "max_tokens": ctx.provider.max_tokens,
        "temperature": ctx.provider.temperature,
        "messages": [{"role": "user", "content": ctx.prompt}],
    }
    if ctx.system_prompt:
        kwargs["system"] = ctx.system_prompt
    if stream:
        kwargs["stream"] = True
    return kwargs


def _run_anthropic_sync(ctx: _APIRunContext) -> str:
    response = ctx.client.messages.create(**_anthropic_request_kwargs(ctx))
    return "".join(
        str(getattr(block, "text", "") or "")
        for block in (getattr(response, "content", None) or [])
    ).strip()


def _run_anthropic_stream(ctx: _APIRunContext) -> str:
    parts: list[str] = []
    last_emit_at = 0.0
    _emit_llm_heartbeat(ctx, "", final=False)
    stream = ctx.client.messages.create(**_anthropic_request_kwargs(ctx, stream=True))
    for event in stream:
        piece = ""
        event_type = str(getattr(event, "type", "") or "")
        if event_type == "content_block_delta":
            piece = str(getattr(getattr(event, "delta", None), "text", "") or "")
        elif event_type == "content_block_start":
            piece = str(getattr(getattr(event, "content_block", None), "text", "") or "")
        if piece:
            parts.append(piece)
        now = time.monotonic()
        if piece and (last_emit_at <= 0 or now - last_emit_at >= 1.0):
            _emit_llm_heartbeat(ctx, "".join(parts), final=False)
            last_emit_at = now
    output = "".join(parts).strip()
    if output:
        _emit_llm_heartbeat(ctx, output, final=True)
    return output


@dataclass(frozen=True)
class _APIEndpointStrategy:
    endpoint: str
    sync_runner: Callable[[_APIRunContext], str]
    stream_runner: Callable[[_APIRunContext], str]

    def run(self, ctx: _APIRunContext) -> str:
        should_stream = _should_stream_llm_progress()
        output = self._run_with_optional_stream(ctx, should_stream=should_stream)
        return self._require_output(ctx, output)

    def _run_with_optional_stream(self, ctx: _APIRunContext, *, should_stream: bool) -> str:
        if not should_stream:
            return self.sync_runner(ctx)
        try:
            return self.stream_runner(ctx)
        except Exception:
            return self.sync_runner(ctx)

    def _require_output(self, ctx: _APIRunContext, output: str) -> str:
        if not output:
            raise RuntimeError(f"{ctx.provider.name} 返回了空内容")
        return output


_API_ENDPOINT_STRATEGIES = {
    "chat.completions": _APIEndpointStrategy(
        endpoint="chat.completions",
        sync_runner=_run_openai_sync,
        stream_runner=_run_openai_stream,
    ),
    "messages": _APIEndpointStrategy(
        endpoint="messages",
        sync_runner=_run_anthropic_sync,
        stream_runner=_run_anthropic_stream,
    ),
}


def _resolve_api_endpoint_strategy(endpoint: str) -> _APIEndpointStrategy:
    strategy = _API_ENDPOINT_STRATEGIES.get(endpoint)
    if strategy is None:
        raise ValueError(f"未知端点类型: {endpoint}")
    return strategy


def _run_api_endpoint(ctx: _APIRunContext, endpoint: str) -> str:
    return _resolve_api_endpoint_strategy(endpoint).run(ctx)


def _run_api_provider(
    provider: APIProvider,
    prompt: str,
    system_prompt: Optional[str] = None,
) -> str:
    """执行 API Provider."""
    client, endpoint = provider.build_client()
    profile = _build_api_request_profile(provider, prompt, system_prompt)
    ctx = _APIRunContext(
        provider=provider,
        client=client,
        prompt=prompt,
        system_prompt=system_prompt,
        messages=_build_api_messages(prompt, system_prompt),
        started_at=time.monotonic(),
        model=profile.model,
        difficulty=profile.difficulty,
        thinking=profile.thinking,
        reasoning_effort=profile.reasoning_effort,
    )

    try:
        return _run_api_endpoint(ctx, endpoint)
    except Exception as e:
        raise RuntimeError(f"{provider.name} API 调用失败: {e}")


def fetch_provider_balance(provider: APIProvider, *, timeout: float = 4.0) -> dict[str, Any]:
    """Fetch balance for providers that expose a compatible balance endpoint."""
    endpoint = str(getattr(provider, "balance_endpoint", "") or "").strip()
    if not endpoint:
        return {"available": False, "error": "provider does not expose balance endpoint"}
    api_key = provider.resolve_api_key()
    if provider.requires_api_key() and not api_key:
        return {"available": False, "error": "missing api key"}
    base_url = (provider.base_url or "https://api.deepseek.com").rstrip("/")
    url = endpoint if endpoint.startswith("http") else f"{base_url}{endpoint}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    payload = json.loads(raw or "{}")
    if isinstance(payload, dict):
        payload.setdefault("available", True)
        return payload
    return {"available": False, "error": "invalid balance response"}



