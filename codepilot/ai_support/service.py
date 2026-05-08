"""AI orchestration: planner (schema prompts), task breakdown, agent selection.

Providers, prompts and classifier live in companion modules and are re-exported
here so existing `from codepilot.ai_support.service import X` imports keep working.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from codepilot.core.config import load_project_config

# Re-export everything from companion modules so the public API is unchanged.
from codepilot.ai_support.providers import (  # noqa: F401 (re-export)
    ANTHROPIC_AVAILABLE,
    API_PROVIDERS,
    APIProvider,
    CLI_PROVIDERS,
    CLIProvider,
    OPENAI_AVAILABLE,
    _collect_project_context,
    _configured_cli_command,
    _detect_shell,
    _ensure_claude_git_bash_env,
    _get_node_modules_path,
    _load_project_config,
    _run_api_provider,
    _run_cli_provider,
    mark_provider_unavailable,
    resolve_api_provider,
    resolve_cli_provider,
)
from codepilot.ai_support.prompts import (  # noqa: F401 (re-export)
    AgentConfig,
    TASK_PROMPT_TEMPLATE,
)
from codepilot.ai_support.classifier import (  # noqa: F401 (re-export)
    _heuristic_intent,
    answer_question_via_api,
    classify_intent,
)
from codepilot.ai_support.result_parse import (
    extract_error_hint as _extract_error_hint_core,
)
from codepilot.ai_support.main_resolution import (
    resolve_task_content_call as _main_resolve_task_content_call,
)
from codepilot.ai_support.main_execute import (
    execute_task_content_call as _main_execute_task_content_call,
)
from codepilot.ai_support import planner_execution as _planner_execution
from codepilot.ai_support import task_planning as _task_planning
from codepilot.core.text_decode import decode_subprocess_text

# ── Module-level state (kept here so monkeypatch in tests keeps working) ─────
# Set by external code (e.g. WebUI) to receive planner stderr lines in real time.
# Signature: (line: str) -> None
_planner_progress_callback: Optional[callable] = None

# Test stub hook: injected to replace real CLI calls in e2e tests.
# Signature: (task: dict, project_path: Path, phase: str, prompt: str) -> tuple[str, int, str]
_phase_stub: Optional[callable] = None


BUILTIN_PHASE_AGENTS = {
    "codex",
    "claude",
    "claude-node",
    "claude-sonnet",
    "claude-opus",
    "claude-haiku",
    "opencode",
}


def normalize_agent_name(name: str) -> str:
    """规范化 agent 名称，尝试找到匹配的 provider."""
    name = name.lower().strip()
    if name == "dual":
        return "dual"

    # CLI family registry takes priority — keeps aliases for new families
    # (opencode/oc, …) in one place. Only return when the family also has
    # a registered CLI provider, otherwise fall through to fuzzy matching.
    from codepilot.ai_support.cli_families import get_family

    family = get_family(name)
    if family is not None and family.provider_key in CLI_PROVIDERS:
        return family.name

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


def is_builtin_phase_agent_supported(agent: str) -> bool:
    """Return whether ``agent`` can drive a builtin builder/reviewer phase."""
    return normalize_agent_name(agent) in BUILTIN_PHASE_AGENTS


def resolve_dual_phase_agents(
    project_path: str | Path | dict | None = None,
    *,
    builder: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve the effective builder/reviewer pair for ``dual`` mode.

    Resolution order is: explicit override -> AGENTS.toml -> built-in defaults.
    ``builder``/``reviewer`` must resolve to concrete agents, not ``dual``.
    """
    cfg = load_project_config(project_path)
    builder_agent = normalize_agent_name(builder or (cfg.builder if cfg and cfg.builder else "codex"))
    reviewer_agent = normalize_agent_name(reviewer or (cfg.reviewer if cfg and cfg.reviewer else "claude"))

    if builder_agent == "dual" or reviewer_agent == "dual":
        raise ValueError("dual 模式的 [agents].builder / reviewer 不能再配置为 dual。")

    return builder_agent, reviewer_agent


def generate_task_content(
    title: str,
    project_path: str = "",
    agent: str = "codex",
    api_keys: Optional[dict[str, str]] = None,
    config_ref: str | Path | dict | None = None,
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
    resolved = _main_resolve_task_content_call(
        title,
        project_path=project_path,
        agent=agent,
        api_keys=api_keys,
        config_ref=config_ref,
        normalize_agent_name=normalize_agent_name,
        check_provider_availability=check_provider_availability,
        collect_project_context=_collect_project_context,
        task_prompt_template=TASK_PROMPT_TEMPLATE,
        api_provider_keys=set(API_PROVIDERS.keys()),
        cli_provider_keys=set(CLI_PROVIDERS.keys()),
    )

    return _main_execute_task_content_call(
        resolved,
        resolve_api_provider=resolve_api_provider,
        resolve_cli_provider=resolve_cli_provider,
        run_api_provider=_run_api_provider,
        run_cli_provider=_run_cli_provider,
        get_node_modules_path=_get_node_modules_path,
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


def check_provider_availability(agent: str, project_path: str | Path | dict | None = None) -> tuple[bool, str]:
    """
    检查 provider 是否可用。

    Returns:
        (is_available, message)
    """
    normalized = normalize_agent_name(agent)

    if normalized == "dual":
        try:
            builder_agent, reviewer_agent = resolve_dual_phase_agents(project_path)
        except ValueError as exc:
            return False, str(exc)

        unsupported = [
            phase
            for phase, resolved in (("builder", builder_agent), ("reviewer", reviewer_agent))
            if not is_builtin_phase_agent_supported(resolved)
        ]
        if unsupported:
            details = ", ".join(
                f"{phase}={builder_agent if phase == 'builder' else reviewer_agent}"
                for phase in unsupported
            )
            return (
                False,
                "当前无法使用 dual 模式："
                f"{details} 超出内置执行器支持范围。"
                "请改用 codex、claude、claude-node、claude-sonnet、claude-opus、claude-haiku 或 opencode。",
            )

        builder_ok, builder_msg = check_provider_availability(builder_agent, project_path=project_path)
        reviewer_ok, reviewer_msg = check_provider_availability(reviewer_agent, project_path=project_path)
        if builder_ok and reviewer_ok:
            return True, (
                "可用: dual 模式将使用 "
                f"{builder_agent} 负责实现、{reviewer_agent} 负责审查"
            )
        missing = []
        if not builder_ok:
            missing.append(f"builder={builder_agent}: {builder_msg}")
        if not reviewer_ok:
            missing.append(f"reviewer={reviewer_agent}: {reviewer_msg}")
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
        provider = resolve_api_provider(normalized, project_path)
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
    project_path: str | Path | dict | None = None,
    default_mode: str = "dual",
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
    """Compatibility wrapper: keep ai._extract_error_hint import path stable."""
    return _extract_error_hint_core(raw)


def _decode_planner_chunk(chunk: str | bytes | None) -> str:
    """Decode planner stdout/stderr chunks with pragmatic Windows fallbacks."""
    return decode_subprocess_text(chunk)




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
    return _planner_execution.run_claude_schema_prompt(
        prompt,
        schema,
        planner=planner,
        project_path=project_path,
        config_ref=config_ref,
        timeout=timeout,
        normalize_agent_name=normalize_agent_name,
        resolve_cli_provider=resolve_cli_provider,
        get_node_modules_path=_get_node_modules_path,
        subprocess_module=subprocess,
        planner_process_group_kwargs_fn=_planner_process_group_kwargs,
        decode_planner_chunk=_decode_planner_chunk,
        kill_process_tree_fn=_kill_process_tree,
        terminate_planner_process_fn=_terminate_planner_process,
        extract_error_hint=_extract_error_hint,
        get_progress_callback=lambda: _planner_progress_callback,
    )


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children. Works on Windows and Unix."""
    _planner_execution.kill_process_tree(pid)


def _planner_process_group_kwargs() -> dict:
    """Isolate planner child processes so timeouts/interrupts can be cleaned up safely.

    Also suppresses Windows console pop-ups when the parent process has no
    console (e.g. when the planner runs inside the detached `codepilot ui start`
    service). Delegates to :func:`runtime.no_window_kwargs`.
    """
    return _planner_execution.planner_process_group_kwargs()


def _terminate_planner_process(process) -> None:
    """Best-effort cleanup for planner subprocesses left running by timeouts/interruption."""
    _planner_execution.terminate_planner_process(
        process,
        kill_process_tree_fn=_kill_process_tree,
    )




def _run_codex_schema_prompt(
    prompt: str,
    schema: dict,
    *,
    project_path: str = "",
    config_ref: str | Path | None = None,
    timeout: int = 240,
) -> dict:
    """Use Codex CLI with a JSON schema output contract."""
    return _planner_execution.run_codex_schema_prompt(
        prompt,
        schema,
        project_path=project_path,
        config_ref=config_ref,
        timeout=timeout,
        check_provider_availability=check_provider_availability,
        resolve_cli_provider=resolve_cli_provider,
        subprocess_module=subprocess,
        planner_process_group_kwargs_fn=_planner_process_group_kwargs,
        decode_planner_chunk=_decode_planner_chunk,
        kill_process_tree_fn=_kill_process_tree,
        terminate_planner_process_fn=_terminate_planner_process,
        extract_error_hint=_extract_error_hint,
        get_progress_callback=lambda: _planner_progress_callback,
    )


def _run_opencode_schema_prompt(
    prompt: str,
    schema: dict,
    *,
    project_path: str = "",
    config_ref: str | Path | None = None,
    timeout: int = 240,
) -> dict:
    """Use OpenCode CLI as the bottom-tier fallback planner.

    Auto-selects the LLM backend (anthropic / deepseek / openai-compat) by
    inspecting the user's configured ``[providers.*]`` keys, then invokes
    ``opencode run`` headless with a schema-baked prompt.
    """
    from codepilot.core.config import load_project_config

    return _planner_execution.run_opencode_schema_prompt(
        prompt,
        schema,
        project_path=project_path,
        config_ref=config_ref,
        timeout=timeout,
        cfg_loader=lambda ref: load_project_config(ref),
        resolve_cli_provider=resolve_cli_provider,
        subprocess_module=subprocess,
        planner_process_group_kwargs_fn=_planner_process_group_kwargs,
        decode_planner_chunk=_decode_planner_chunk,
        kill_process_tree_fn=_kill_process_tree,
        terminate_planner_process_fn=_terminate_planner_process,
        extract_error_hint=_extract_error_hint,
        get_progress_callback=lambda: _planner_progress_callback,
    )


def build_task_markdown_from_plan(task: dict) -> str:
    """Convert a structured plan item into task markdown using task templates."""
    return _task_planning.build_task_markdown_from_plan(
        task,
        normalize_agent_name=normalize_agent_name,
        template_path=Path(__file__).resolve().parent.parent / "templates" / "task-template.md",
    )




def _run_recon_stage(
    title: str,
    project_path: str,
    *,
    planner_normalized: str,
    config_ref: str | Path | None,
    project_context: str,
    progress_prefix: str = "  [recon]",
) -> dict:
    """Run the reconnaissance stage: let the planner read the project before planning."""
    from codepilot.ai_support.planner_context import validate_recon_payload

    return _task_planning.run_recon_stage(
        title,
        project_path,
        planner_normalized=planner_normalized,
        config_ref=config_ref,
        project_context=project_context,
        progress_prefix=progress_prefix,
        run_claude_schema_prompt=_run_claude_schema_prompt,
        run_codex_schema_prompt=_run_codex_schema_prompt,
        run_opencode_schema_prompt=_run_opencode_schema_prompt,
        validate_recon_payload=validate_recon_payload,
        get_progress_callback=lambda: _planner_progress_callback,
    )


def _format_recon_block(recon: dict) -> str:
    """Render recon payload as a readable block for the planner prompt."""
    return _task_planning.format_recon_block(recon)


def parse_automation_planner_result(
    breakdown: dict | str,
    *,
    title: str,
    max_tasks: int = 5,
    existing_tasks: Optional[list[dict]] = None,
) -> dict:
    """Normalize planner output via the dedicated parser module."""
    return _task_planning.parse_automation_planner_result(
        breakdown,
        title=title,
        max_tasks=max_tasks,
        existing_tasks=existing_tasks,
        progress_callback=_planner_progress_callback,
    )


def generate_task_breakdown(
    title: str,
    project_path: str = "",
    planner: str = "codex",
    max_tasks: int = 5,
    config_ref: str | Path | None = None,
    *,
    two_stage: bool = True,
    parse_result: bool = True,
    existing_tasks: Optional[list[dict]] = None,
) -> dict:
    """Generate a structured subtask breakdown for a high-level goal."""
    from codepilot.ai_support.backlog_dedup import format_existing_block
    from codepilot.ai_support.planner_context import collect_planner_context

    return _task_planning.generate_task_breakdown(
        title,
        project_path=project_path,
        planner=planner,
        max_tasks=max_tasks,
        config_ref=config_ref,
        two_stage=two_stage,
        parse_result=parse_result,
        existing_tasks=existing_tasks,
        normalize_agent_name=normalize_agent_name,
        collect_planner_context=collect_planner_context,
        format_existing_block=format_existing_block,
        run_recon_stage_fn=_run_recon_stage,
        format_recon_block_fn=_format_recon_block,
        run_claude_schema_prompt=_run_claude_schema_prompt,
        run_codex_schema_prompt=_run_codex_schema_prompt,
        run_opencode_schema_prompt=_run_opencode_schema_prompt,
        parse_automation_planner_result_fn=parse_automation_planner_result,
    )



