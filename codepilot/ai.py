"""AI orchestration: planner (schema prompts), task breakdown, agent selection.

Providers, prompts and classifier live in companion modules and are re-exported
here so existing `from codepilot.ai import X` imports keep working.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

from codepilot.config import load_project_config

# Re-export everything from companion modules so the public API is unchanged.
from codepilot.ai_providers import (  # noqa: F401 (re-export)
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
    resolve_cli_provider,
)
from codepilot.ai_prompts import (  # noqa: F401 (re-export)
    AgentConfig,
    TASK_BREAKDOWN_PROMPT_TEMPLATE,
    TASK_BREAKDOWN_SCHEMA,
    TASK_PROMPT_TEMPLATE,
)
from codepilot.ai_classifier import (  # noqa: F401 (re-export)
    _heuristic_intent,
    answer_question_via_api,
    classify_intent,
)

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
}


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


def is_builtin_phase_agent_supported(agent: str) -> bool:
    """Return whether ``agent`` can drive a builtin builder/reviewer phase."""
    return normalize_agent_name(agent) in BUILTIN_PHASE_AGENTS


def resolve_dual_phase_agents(
    project_path: str | Path | None = None,
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
                "请改用 codex、claude、claude-node、claude-sonnet、claude-opus 或 claude-haiku。",
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
        "--dangerously-skip-permissions",
    ])
    if model_alias:
        cmd.extend(["--model", model_alias])
    # -p "prompt" 必须放最后，否则 claude CLI 会忽略 --json-schema
    cmd.extend(["-p", prompt])

    process = None
    try:
        import threading, sys, time as _time

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_planner_process_group_kwargs(),
        )

        stdout_chunks: list[str] = []
        last_activity = [_time.monotonic()]

        # stderr 线程实时打印 claude 进度
        def _stream_stderr():
            assert process.stderr is not None
            for line in process.stderr:
                stripped = line.rstrip()
                if stripped:
                    last_activity[0] = _time.monotonic()
                    sys.stderr.write(f"  [planner] {stripped}\n")
                    sys.stderr.flush()
                    if _planner_progress_callback:
                        try:
                            _planner_progress_callback(stripped)
                        except Exception:
                            pass

        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stderr_thread.start()

        def _read_stdout():
            assert process.stdout is not None
            stdout_chunks.append(process.stdout.read())

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stdout_thread.start()

        # Wait with idle-based stall detection.
        # Semantics: `timeout` is the MAX IDLE TIME (no new output) before we
        # kill the child. As long as claude keeps printing progress to stderr,
        # the deadline keeps sliding. A separate hard cap protects against
        # runaway processes that never stop printing either.
        started = _time.monotonic()
        hard_cap = max(timeout * 10, 1800)  # absolute safety net
        stall_warned = False
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break
            now = _time.monotonic()
            elapsed = now - started
            idle = now - last_activity[0]

            # Hard cap (only to stop a truly runaway process)
            if elapsed > hard_cap:
                _kill_process_tree(process.pid)
                process.wait(timeout=5)
                raise subprocess.TimeoutExpired(cmd, hard_cap)

            # Idle kill — silent for too long → consider hung
            if idle > timeout:
                _kill_process_tree(process.pid)
                process.wait(timeout=5)
                raise subprocess.TimeoutExpired(cmd, timeout)

            # Heartbeat warning once per stall period
            if idle > 60 and not stall_warned:
                stall_warned = True
                msg = f"claude 已 {int(idle)}s 无输出（idle 超过 {timeout}s 会强制结束）..."
                sys.stderr.write(f"  [planner] {msg}\n")
                sys.stderr.flush()
                if _planner_progress_callback:
                    try:
                        _planner_progress_callback(msg)
                    except Exception:
                        pass
            elif idle < 5 and stall_warned:
                # Output resumed — allow a fresh warning next stall
                stall_warned = False

            _time.sleep(0.5)

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=2)

        result_stdout = "".join(stdout_chunks)
        result_returncode = process.returncode
    except subprocess.TimeoutExpired as exc:
        _terminate_planner_process(process)
        raise RuntimeError(
            f"{provider.name} 在任务拆分阶段已连续 {timeout}s 没有任何输出，视为卡住并强制终止。"
            "可以稍后重试，或改用 codex 作为规划器。"
        ) from exc
    except KeyboardInterrupt as exc:
        _terminate_planner_process(process)
        raise RuntimeError(
            f"{provider.name} 在任务拆分阶段被中断，已终止当前规划。可以稍后重试。"
        ) from exc
    except BaseException:
        _terminate_planner_process(process)
        raise

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


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children. Works on Windows and Unix."""
    if platform.system().lower() == "windows":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def _planner_process_group_kwargs() -> dict:
    """Isolate planner child processes so timeouts/interrupts can be cleaned up safely.

    Also suppresses Windows console pop-ups when the parent process has no
    console (e.g. when the planner runs inside the detached `codepilot webui`
    service). Delegates to :func:`runtime.no_window_kwargs`.
    """
    from codepilot.runtime import no_window_kwargs
    return no_window_kwargs(new_process_group=True)


def _terminate_planner_process(process) -> None:
    """Best-effort cleanup for planner subprocesses left running by timeouts/interruption."""
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if pid:
        try:
            _kill_process_tree(int(pid))
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass




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

        process = None
        try:
            import threading as _threading
            import time as _time

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_planner_process_group_kwargs(),
            )
            if process.stdin:
                process.stdin.write(prompt)
                process.stdin.close()

            # Use separate threads for BOTH stdout and stderr (avoid communicate() deadlock)
            stdout_chunks: list[str] = []
            last_activity = [_time.monotonic()]  # mutable for closure

            def _read_codex_stdout():
                assert process.stdout is not None
                stdout_chunks.append(process.stdout.read())

            def _stream_codex_stderr():
                assert process.stderr is not None
                for line in process.stderr:
                    stripped = line.rstrip()
                    if stripped:
                        last_activity[0] = _time.monotonic()
                        sys.stderr.write(f"  [planner] {stripped}\n")
                        sys.stderr.flush()
                        if _planner_progress_callback:
                            try:
                                _planner_progress_callback(stripped)
                            except Exception:
                                pass

            stdout_thread = _threading.Thread(target=_read_codex_stdout, daemon=True)
            stderr_thread = _threading.Thread(target=_stream_codex_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            # Wait with idle-based stall detection.
            # `timeout` = MAX IDLE TIME (no new output) before we kill.
            # As long as codex keeps printing, the deadline slides.
            started = _time.monotonic()
            hard_cap = max(timeout * 10, 1800)
            stall_warned = False
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    break
                now = _time.monotonic()
                elapsed = now - started
                idle = now - last_activity[0]

                if elapsed > hard_cap:
                    _kill_process_tree(process.pid)
                    process.wait(timeout=5)
                    raise subprocess.TimeoutExpired(cmd, hard_cap)

                if idle > timeout:
                    _kill_process_tree(process.pid)
                    process.wait(timeout=5)
                    raise subprocess.TimeoutExpired(cmd, timeout)

                if idle > 60 and not stall_warned:
                    stall_warned = True
                    msg = f"codex 已 {int(idle)}s 无输出（idle 超过 {timeout}s 会强制结束）..."
                    sys.stderr.write(f"  [planner] {msg}\n")
                    sys.stderr.flush()
                    if _planner_progress_callback:
                        try:
                            _planner_progress_callback(msg)
                        except Exception:
                            pass
                elif idle < 5 and stall_warned:
                    stall_warned = False  # output resumed

                _time.sleep(0.5)

            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=2)
            codex_returncode = process.returncode
            codex_stdout = "".join(stdout_chunks)
            codex_stderr = ""
        except subprocess.TimeoutExpired as exc:
            _terminate_planner_process(process)
            raise RuntimeError(
                f"Codex 在任务拆分阶段已连续 {timeout}s 没有任何输出，视为卡住并强制终止。"
                "可以稍后重试，或改用 claude 作为规划器。"
            ) from exc
        except KeyboardInterrupt as exc:
            _terminate_planner_process(process)
            raise RuntimeError("Codex 在任务拆分阶段被中断，已终止当前规划。可以稍后重试。") from exc
        except BaseException:
            _terminate_planner_process(process)
            raise

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




def _run_recon_stage(
    title: str,
    project_path: str,
    *,
    planner_normalized: str,
    config_ref: str | Path | None,
    project_context: str,
    progress_prefix: str = "  [recon]",
) -> dict:
    """Run the reconnaissance stage: let the planner read the project before planning.

    Returns a dict matching :data:`RECON_SCHEMA`. Errors are swallowed and
    replaced with an empty-but-valid result so the planning stage still runs
    (better to plan with less context than to fail the whole workflow).
    """
    from codepilot.ai_prompts import RECON_PROMPT_TEMPLATE, RECON_SCHEMA

    prompt = RECON_PROMPT_TEMPLATE.format(
        title=title,
        project_context=project_context or "（无项目上下文，自己用工具探索）",
    )

    cb = _planner_progress_callback
    if cb:
        try:
            cb(f"{progress_prefix} 启动侦察：读取相关文件，梳理现状...")
        except Exception:
            pass

    try:
        if planner_normalized in {"claude", "claude-node", "claude-sonnet", "claude-opus", "claude-haiku"}:
            payload = _run_claude_schema_prompt(
                prompt,
                RECON_SCHEMA,
                planner=planner_normalized,
                project_path=project_path,
                config_ref=config_ref,
            )
        elif planner_normalized == "codex":
            payload = _run_codex_schema_prompt(
                prompt,
                RECON_SCHEMA,
                project_path=project_path,
                config_ref=config_ref,
            )
        else:
            payload = {}
    except Exception as exc:
        if cb:
            try:
                cb(f"{progress_prefix} 侦察失败，跳过直接进规划：{exc}")
            except Exception:
                pass
        return {}

    if not isinstance(payload, dict):
        return {}

    # Sanitize hallucinated paths. Any file the recon claims we should
    # touch has to actually exist on disk; otherwise downstream tasks will
    # carry forward fake paths and the executor will choke. When validation
    # drops everything we backfill with keyword-matched real files.
    from codepilot.ai_planner_context import validate_recon_payload

    cleaned, dropped = validate_recon_payload(payload, project_path, title=title)
    if cb:
        try:
            kept = cleaned.get("relevant_files") or []
            if dropped:
                cb(
                    f"{progress_prefix} 侦察完成：认定 {len(kept)} 个相关文件；"
                    f"丢弃 {len(dropped)} 个不存在的路径（{', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''}）"
                )
            else:
                cb(f"{progress_prefix} 侦察完成：认定 {len(kept)} 个相关文件")
        except Exception:
            pass
    return cleaned


def _format_recon_block(recon: dict) -> str:
    """Render recon payload as a readable block for the planner prompt."""
    if not recon:
        return "（本次未生成侦察结论；请基于项目上下文自行判断。）"
    lines: list[str] = []
    current = (recon.get("current_state") or "").strip()
    if current:
        lines.append(f"现状：{current}")
    files = [f for f in (recon.get("relevant_files") or []) if isinstance(f, str) and f.strip()]
    if files:
        lines.append("相关文件：")
        for f in files[:12]:
            lines.append(f"  - {f}")
    findings = [x for x in (recon.get("key_findings") or []) if isinstance(x, str) and x.strip()]
    if findings:
        lines.append("关键发现：")
        for x in findings[:8]:
            lines.append(f"  - {x}")
    risks = [x for x in (recon.get("risks") or []) if isinstance(x, str) and x.strip()]
    if risks:
        lines.append("风险：")
        for x in risks[:6]:
            lines.append(f"  - {x}")
    approach = (recon.get("suggested_approach") or "").strip()
    if approach:
        lines.append(f"建议路径：{approach}")
    return "\n".join(lines) if lines else "（侦察返回为空。）"


def parse_automation_planner_result(
    breakdown: dict | str,
    *,
    title: str,
    max_tasks: int = 5,
    existing_tasks: Optional[list[dict]] = None,
) -> dict:
    """Normalize, validate and deduplicate automation planner output.

    Accepts either the already-decoded planner payload or a raw JSON string.
    This keeps ``generate_task_breakdown`` and higher-level workflow entry
    points on the same parsing rules.
    """
    from codepilot.ai_backlog_dedup import filter_duplicate_tasks

    if isinstance(breakdown, str):
        try:
            breakdown = json.loads(breakdown)
        except json.JSONDecodeError as exc:
            raise RuntimeError("自动规划返回的内容不是有效 JSON，暂时无法继续自动规划。") from exc

    if not isinstance(breakdown, dict):
        raise RuntimeError("自动规划返回的结果格式不正确，暂时无法继续自动规划。")

    raw_tasks = breakdown.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise RuntimeError("任务拆分结果为空")

    max_tasks = max(1, min(max_tasks, 8))

    _garbage_keywords_zh = ("等待", "请提供", "请输入", "待用户", "请发送", "等待高层")
    _garbage_keywords_en = ("awaiting", "waiting for", "please provide", "no goal", "user input needed", "awaiting-user")

    def _is_garbage_task(task_item: object) -> bool:
        if not isinstance(task_item, dict):
            return True
        t = (task_item.get("title") or "").strip()
        g = (task_item.get("goal") or "").strip()
        combined = t + " " + g
        combined_lower = combined.lower()
        for kw in _garbage_keywords_zh:
            if kw in combined:
                return True
        for kw in _garbage_keywords_en:
            if kw in combined_lower:
                return True
        if t.lower() in ("awaiting-user-input", "waiting", "pending", "no-op", "placeholder"):
            return True
        return not t

    valid_tasks = [dict(task_item) for task_item in raw_tasks if not _is_garbage_task(task_item)]
    if not valid_tasks:
        summary = str(breakdown.get("summary", ""))
        rejected = "\n".join(
            f"  - {(item.get('title') or '?')[:60]} :: {(item.get('goal') or '?')[:80]}"
            for item in raw_tasks[:3]
            if isinstance(item, dict)
        ) or "  - （无可展示任务）"
        raise RuntimeError(
            f"规划器把这次需求理解成「等待 / 请用户补充」一类的占位任务，全部被过滤掉了。\n"
            f"规划器摘要：{summary[:200]}\n"
            f"被过滤的任务示例：\n{rejected}\n"
            f"通常出现在需求过于宽泛 / 探索性时（比如「看看有没有什么优化点」）。建议：\n"
            f"  1. 把需求写得更具体：指明要修改 / 新增 / 优化哪一块；\n"
            f"  2. 想让 AI 主动找改进点：用 `codepilot inspect -p <项目>`；\n"
            f"  3. 或换 codex 规划器（自带兜底降级），用 --planner codex 重试。"
        )
    if len(valid_tasks) < len(raw_tasks):
        dropped = len(raw_tasks) - len(valid_tasks)
        sys.stderr.write(f"  [planner] 过滤掉 {dropped} 个无效任务\n")

    import re as _re

    _req_words = set(_re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]{3,}", title.lower()))
    _req_words -= {"the", "and", "for", "that", "with", "this", "from", "into",
                   "can", "not", "but", "all", "will", "have", "are", "was",
                   "then", "just", "one", "also", "use", "using", "some",
                   "about", "what", "which", "how", "been", "more", "when"}
    if _req_words:
        for task_item in valid_tasks:
            task_text = (
                (task_item.get("title") or "") + " " +
                (task_item.get("goal") or "") + " " +
                " ".join(task_item.get("files") or [])
            ).lower()
            task_words = set(_re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]{3,}", task_text))
            overlap = _req_words & task_words
            if not overlap:
                sys.stderr.write(
                    f"  [planner] 警告: 任务 '{task_item.get('title', '')[:40]}' "
                    f"与需求无明显关联, 可能跑偏\n"
                )

    capped_tasks = valid_tasks[:max_tasks]

    dedup_skipped: list[dict] = []
    for item in breakdown.get("dedup_skipped") or []:
        if isinstance(item, dict):
            dedup_skipped.append({
                "proposed_title": item.get("proposed_title"),
                "matched_existing_id": item.get("matched_existing_id"),
                "matched_existing_title": item.get("matched_existing_title"),
            })

    if existing_tasks:
        kept_tasks, dropped_tasks = filter_duplicate_tasks(capped_tasks, existing_tasks)
        if dropped_tasks:
            progress_cb = _planner_progress_callback
            seen = {
                (item.get("proposed_title"), item.get("matched_existing_id"), item.get("matched_existing_title"))
                for item in dedup_skipped
            }
            for dup in dropped_tasks:
                msg = (
                    f"  [planner] 跳过重复任务 '{(dup.get('title') or '')[:40]}' "
                    f"（已存在 #{dup.get('_dedup_matched_id')} "
                    f"'{(dup.get('_dedup_matched_title') or '')[:40]}'）"
                )
                sys.stderr.write(msg + "\n")
                if progress_cb:
                    try:
                        progress_cb(msg.strip())
                    except Exception:
                        pass

                normalized_dup = {
                    "proposed_title": dup.get("title"),
                    "matched_existing_id": dup.get("_dedup_matched_id"),
                    "matched_existing_title": dup.get("_dedup_matched_title"),
                }
                dedup_key = (
                    normalized_dup["proposed_title"],
                    normalized_dup["matched_existing_id"],
                    normalized_dup["matched_existing_title"],
                )
                if dedup_key not in seen:
                    dedup_skipped.append(normalized_dup)
                    seen.add(dedup_key)
            capped_tasks = kept_tasks

    normalized = dict(breakdown)
    normalized["tasks"] = capped_tasks
    normalized["complexity"] = normalized.get("complexity") or (
        "simple" if len(normalized["tasks"]) <= 1 else "complex"
    )
    normalized["should_split"] = bool(
        normalized.get("should_split")
        if normalized.get("should_split") is not None
        else len(normalized["tasks"]) > 1
    )
    if dedup_skipped:
        normalized["dedup_skipped"] = dedup_skipped
    else:
        normalized.pop("dedup_skipped", None)
    return normalized


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
    """Generate a structured subtask breakdown for a high-level goal.

    When ``two_stage`` is True (default) the planner runs a preliminary
    reconnaissance stage where it is encouraged to read relevant project
    files, then the breakdown stage consumes the recon output. Setting it to
    False falls back to a single-shot planning call (legacy behavior).

    When ``existing_tasks`` is provided (open backlog + in-progress rows for
    the project), they are surfaced to the planner and used to filter
    near-duplicate titles after the fact. Pass ``None`` from unit tests that
    don't care about dedup.
    """
    from codepilot.ai_backlog_dedup import format_existing_block
    from codepilot.ai_planner_context import collect_planner_context

    max_tasks = max(1, min(max_tasks, 8))
    normalized = normalize_agent_name(planner)
    context = collect_planner_context(project_path, title)

    recon: dict = {}
    if two_stage:
        recon = _run_recon_stage(
            title,
            project_path,
            planner_normalized=normalized,
            config_ref=config_ref,
            project_context=context,
        )

    recon_block = _format_recon_block(recon)
    existing_block = format_existing_block(existing_tasks)
    prompt = TASK_BREAKDOWN_PROMPT_TEMPLATE.format(
        title=title,
        project_context=context or "（上下文收集失败，按用户需求尽力拆分）",
        recon_block=recon_block,
        existing_tasks_block=existing_block,
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
    if not parse_result:
        return breakdown

    return parse_automation_planner_result(
        breakdown,
        title=title,
        max_tasks=max_tasks,
        existing_tasks=existing_tasks,
    )


