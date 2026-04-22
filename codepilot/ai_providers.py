"""Provider dataclasses, registries, shell/env helpers and runners.

Split out from ai.py for maintainability. All symbols are re-exported by
`codepilot.ai` so existing `from codepilot.ai import CLIProvider` keeps working.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import importlib.util
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from codepilot.ai_planner_context import collect_planner_context
from codepilot.config import load_project_config
from codepilot.text_decode import decode_subprocess_text

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
    if provider_cfg.base_url:
        provider.base_url = provider_cfg.base_url.strip()
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


