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
    resolve_api_provider,
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
from codepilot.ai_result_parse import (
    extract_error_hint as _extract_error_hint_core,
    parse_structured_json_output as _parse_structured_json_output,
)
from codepilot.ai_main_resolution import (
    resolve_task_content_call as _main_resolve_task_content_call,
)
from codepilot.ai_main_execute import (
    execute_task_content_call as _main_execute_task_content_call,
)
from codepilot.text_decode import decode_subprocess_text

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
            **_planner_process_group_kwargs(),
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        last_activity = [_time.monotonic()]

        # stderr 线程实时打印 claude 进度
        def _stream_stderr():
            assert process.stderr is not None
            for line in process.stderr:
                decoded_line = _decode_planner_chunk(line)
                stderr_chunks.append(decoded_line)
                stripped = decoded_line.rstrip()
                if not stripped:
                    continue
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
            stdout_chunks.append(_decode_planner_chunk(process.stdout.read()))

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
        result_stderr = "".join(stderr_chunks)
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
        hint = _extract_error_hint("\n".join(part for part in (result_stderr, result_stdout) if part))
        suffix = f"原因：{hint}" if hint else "请检查 Claude CLI 当前是否可用。"
        raise RuntimeError(f"{provider.name} 没有成功完成任务拆分。{suffix}")

    output = result_stdout.strip()
    if not output:
        raise RuntimeError("Claude 任务拆分返回空内容")
    return _parse_structured_json_output(output, provider_name=provider.name)


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
    console (e.g. when the planner runs inside the detached `codepilot ui start`
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
                **_planner_process_group_kwargs(),
            )
            if process.stdin:
                process.stdin.write(prompt.encode("utf-8"))
                process.stdin.close()

            # Use separate threads for BOTH stdout and stderr (avoid communicate() deadlock)
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            last_activity = [_time.monotonic()]  # mutable for closure

            def _read_codex_stdout():
                assert process.stdout is not None
                for line in process.stdout:
                    decoded_line = _decode_planner_chunk(line)
                    stdout_chunks.append(decoded_line)
                    if decoded_line:
                        last_activity[0] = _time.monotonic()

            def _stream_codex_stderr():
                assert process.stderr is not None
                for line in process.stderr:
                    decoded_line = _decode_planner_chunk(line)
                    stderr_chunks.append(decoded_line)
                    stripped = decoded_line.rstrip()
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
            codex_stderr = "".join(stderr_chunks)
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
            hint = _extract_error_hint("\n".join(part for part in (codex_stderr, codex_stdout) if part))
            suffix = f"原因：{hint}" if hint else "请检查 Codex CLI 当前是否可用。"
            raise RuntimeError(f"Codex 没有成功完成任务拆分。{suffix}")

        output = output_path.read_text(encoding="utf-8", errors="replace").strip() if output_path.exists() else ""
        if not output:
            output = codex_stdout.strip()
    if not output:
        raise RuntimeError("Codex 没有返回任务拆分结果，暂时无法继续自动规划。")
    return _parse_structured_json_output(output, provider_name="Codex")


def build_task_markdown_from_plan(task: dict) -> str:
    """Convert a structured plan item into task markdown using task templates."""

    class _SafeFormat(dict):
        def __missing__(self, key: str) -> str:  # type: ignore[override]
            return ""

    def _bullet(items: list[str], fallback: str) -> str:
        rows = [str(item).strip() for item in (items or []) if str(item).strip()]
        return "\n".join(f"- {row}" for row in rows) if rows else f"- {fallback}"

    def _coerce_list(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        return []

    def _ac_matrix(criteria_items: list[str]) -> str:
        rows = [str(item).strip() for item in (criteria_items or []) if str(item).strip()]
        if not rows:
            rows = ["待补充"]
        header = (
            "| AC # | Criterion | Verification Command / Action | Expected Result | Evidence Location |\n"
            "| :--- | :--- | :--- | :--- | :--- |"
        )
        body_lines = []
        for i, row in enumerate(rows):
            safe = row.replace("|", "\\|")
            body_lines.append(f"| AC-{i + 1} | {safe} |  |  |  |")
        return header + "\n" + "\n".join(body_lines)

    title = str(task.get("title") or "").strip() or "未命名任务"
    goal = str(task.get("goal") or "").strip() or "待补充"
    acceptance_items = _coerce_list(task.get("acceptance_criteria"))
    acceptance = _bullet(acceptance_items, "待补充")
    ac_matrix = _ac_matrix(acceptance_items)
    builder_notes = _bullet(_coerce_list(task.get("builder_notes")), "待补充")
    reviewer_notes = _bullet(_coerce_list(task.get("reviewer_notes")), "待补充")
    files = _bullet(_coerce_list(task.get("files")), "待确认")
    notes = _bullet(_coerce_list(task.get("notes")), "无")
    forbidden = _bullet(_coerce_list(task.get("forbidden")), "不改动任务声明范围外的生产代码；不做无关重构。")
    not_in_scope = _bullet(_coerce_list(task.get("not_in_scope")), "与本任务目标无关的模块、文档、部署流程。")
    priority = str(task.get("priority") or "").strip() or "P2"
    risk_level = str(task.get("risk_level") or "").strip() or "待评估"
    scope_budget = str(task.get("scope_budget") or "").strip() or "未设定"
    owner = str(task.get("owner") or "").strip() or "未指派"
    evidence = str(task.get("evidence") or "").strip() or "（未提供规划依据，建议人工复核）"
    dep_indices_raw = task.get("depends_on_indices")
    dep_indices = (
        [i for i in dep_indices_raw if isinstance(i, int) and i >= 0]
        if isinstance(dep_indices_raw, list)
        else []
    )
    depends_on = "无" if not dep_indices else ", ".join(f"T{idx + 1}" for idx in dep_indices)

    normalized_agent = normalize_agent_name(str(task.get("agent") or "dual"))

    template_path = Path(__file__).resolve().parent / "templates" / "task-template.md"
    template_text = template_path.read_text(encoding="utf-8", errors="replace")
    return template_text.format_map(
        _SafeFormat(
            {
                "title": title,
                "agent": normalized_agent,
                "priority": priority,
                "depends_on": depends_on,
                "risk_level": risk_level,
                "scope_budget": scope_budget,
                "owner": owner,
                "evidence": evidence,
                "goal": goal,
                "criteria": acceptance,
                "ac_matrix": ac_matrix,
                "requirements": builder_notes,
                "builder_responsibilities": builder_notes,
                "reviewer_responsibilities": reviewer_notes,
                "files": files,
                "forbidden": forbidden,
                "not_in_scope": not_in_scope,
                "notes": notes,
            }
        )
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
        project_context=project_context or "(No project context; inspect via tools.)",
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
        return "(No recon conclusions were produced; plan based on project context.)"
    lines: list[str] = []
    current = (recon.get("current_state") or "").strip()
    if current:
        lines.append(f"Current state: {current}")
    files = [f for f in (recon.get("relevant_files") or []) if isinstance(f, str) and f.strip()]
    if files:
        lines.append("Relevant files:")
        for f in files[:12]:
            lines.append(f"  - {f}")
    findings = [x for x in (recon.get("key_findings") or []) if isinstance(x, str) and x.strip()]
    if findings:
        lines.append("Key findings:")
        for x in findings[:8]:
            lines.append(f"  - {x}")
    risks = [x for x in (recon.get("risks") or []) if isinstance(x, str) and x.strip()]
    if risks:
        lines.append("Risks:")
        for x in risks[:6]:
            lines.append(f"  - {x}")
    approach = (recon.get("suggested_approach") or "").strip()
    if approach:
        lines.append(f"Suggested approach: {approach}")
    return "\n".join(lines) if lines else "(Recon output is empty.)"


def parse_automation_planner_result(
    breakdown: dict | str,
    *,
    title: str,
    max_tasks: int = 5,
    existing_tasks: Optional[list[dict]] = None,
) -> dict:
    """Normalize planner output via the dedicated parser module."""
    from codepilot.ai_planner_parse import parse_automation_planner_result as _parse_result

    return _parse_result(
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
        project_context=context or "(Context collection failed; plan from requirement only.)",
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


