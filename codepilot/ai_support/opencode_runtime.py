"""OpenCode CLI runtime support.

OpenCode (sst/opencode) is provider-agnostic — it speaks Anthropic / OpenAI /
OpenAI-compatible endpoints depending on which env vars are populated when the
subprocess starts. This module is the bridge between CodePilot's
``[providers.*]`` config and that env-driven contract.

Selection rule (smart auto-pick):

  1. anthropic         — any of ``claude-opus`` / ``claude-sonnet`` / ``claude-haiku``
                         providers has a non-empty ``api_key`` and is enabled.
                         Injects ``ANTHROPIC_API_KEY``.
  2. openai-compatible — ``deepseek`` provider has a key + ``base_url``.
                         Injects ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL``.
  3. openai            — any ``openai-*`` provider has a key.
                         Injects ``OPENAI_API_KEY`` (+ ``OPENAI_BASE_URL`` only if
                         the provider has a custom ``base_url``).

If none of the above match, :func:`select_opencode_backend` raises
:class:`OpenCodeBackendUnavailable` with an actionable hint so callers can
surface a clear "OpenCode 兜底未启用" diagnostic instead of a generic
subprocess failure later on.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codepilot.core.config import AgentsConfig, ProviderAPIConfig

ANTHROPIC_PROVIDER_KEYS = ("claude-opus", "claude-sonnet", "claude-haiku")
OPENAI_PROVIDER_KEYS = ("openai-gpt4", "openai-gpt4o", "openai-gpt35")
DEEPSEEK_PROVIDER_KEY = "deepseek"


class OpenCodeBackend(enum.Enum):
    """Discriminator for which API style OpenCode should target."""

    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai-compatible"
    OPENAI = "openai"


class OpenCodeBackendUnavailable(RuntimeError):
    """Raised when no [providers.*] entry has a key OpenCode can use."""


@dataclass(frozen=True)
class OpenCodeBackendSelection:
    """Resolved backend choice + the env vars that should be injected."""

    name: str
    env: dict[str, str]
    source_provider: str


def _enabled_provider(cfg: AgentsConfig, name: str) -> ProviderAPIConfig | None:
    provider = cfg.providers.get(name)
    if provider is None:
        return None
    if not provider.enabled:
        return None
    if not (provider.api_key or "").strip():
        return None
    return provider


def select_opencode_backend(cfg: AgentsConfig) -> OpenCodeBackendSelection:
    """Pick the best backend for OpenCode based on configured provider keys.

    Implements the auto-pick chain documented in the module docstring. The
    return value carries both the discriminator and the env-var dict so the
    caller can compose it with the inherited subprocess environment.
    """
    for name in ANTHROPIC_PROVIDER_KEYS:
        provider = _enabled_provider(cfg, name)
        if provider is not None:
            return OpenCodeBackendSelection(
                name=OpenCodeBackend.ANTHROPIC.name,
                env={"ANTHROPIC_API_KEY": provider.api_key.strip()},
                source_provider=name,
            )

    deepseek = _enabled_provider(cfg, DEEPSEEK_PROVIDER_KEY)
    if deepseek is not None:
        env = {"OPENAI_API_KEY": deepseek.api_key.strip()}
        base_url = (deepseek.base_url or "").strip()
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
        return OpenCodeBackendSelection(
            name=OpenCodeBackend.OPENAI_COMPATIBLE.name,
            env=env,
            source_provider=DEEPSEEK_PROVIDER_KEY,
        )

    for name in OPENAI_PROVIDER_KEYS:
        provider = _enabled_provider(cfg, name)
        if provider is not None:
            env = {"OPENAI_API_KEY": provider.api_key.strip()}
            base_url = (provider.base_url or "").strip()
            if base_url:
                env["OPENAI_BASE_URL"] = base_url
            return OpenCodeBackendSelection(
                name=OpenCodeBackend.OPENAI.name,
                env=env,
                source_provider=name,
            )

    raise OpenCodeBackendUnavailable(
        "OpenCode 兜底未启用：[providers.*] 里没有可用的 API key。\n"
        "请在 AGENTS.toml / .codepilot.secrets.toml 里至少配置以下任一项："
        "anthropic（claude-opus/sonnet/haiku 任意一个），"
        "deepseek（含 base_url），"
        "或 openai-gpt4 / openai-gpt4o / openai-gpt35。"
    )


def build_opencode_env(cfg: AgentsConfig) -> dict[str, str]:
    """Return only the env vars OpenCode needs — no other provider keys leak in.

    The caller is expected to merge this dict on top of the inherited process
    environment when launching ``opencode run``.
    """
    return dict(select_opencode_backend(cfg).env)


def wrap_schema_prompt(prompt: str, schema: dict) -> str:
    """Bake a JSON-schema instruction header onto the user prompt.

    OpenCode does not currently expose a native ``--output-schema`` flag, so
    schema-constrained planning is enforced through prompt engineering. The
    caller is expected to parse the model's response with the project's
    standard structured-JSON parser (which already tolerates code-fence and
    leading prose).
    """
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
    return (
        "你必须只输出一个 JSON 对象，符合下述 JSON Schema，不要包含任何额外文本、"
        "Markdown 代码块标记或解释：\n\n"
        "Schema:\n"
        f"{schema_json}\n\n"
        "请基于以下需求生成 JSON：\n\n"
        f"{prompt}\n"
    )


def run_opencode_schema_prompt(
    prompt: str,
    schema: dict,
    *,
    project_path: str = "",
    config_ref: str | Path | None = None,
    timeout: int = 240,
    cfg_loader: Callable[[str | Path | None], AgentsConfig | None],
    resolve_cli_provider: Callable[[str, str | Path | None], Any],
    subprocess_module: Any,
    planner_process_group_kwargs_fn: Callable[[], dict[str, Any]],
    decode_planner_chunk: Callable[[str | bytes | None], str],
    kill_process_tree_fn: Callable[[int], None],
    terminate_planner_process_fn: Callable[[Any], None],
    extract_error_hint: Callable[[str], str],
    get_progress_callback: Callable[[], Callable[[str], None] | None],
) -> dict:
    """Run OpenCode in headless mode and parse a schema-constrained JSON reply.

    Mirrors :func:`codepilot.ai_support.planner_execution.run_codex_schema_prompt`
    in shape (so #212 can fold all three families into one shared runner) but
    diverges on the I/O contract:

    * No ``--output-schema`` flag — the schema is baked into the prompt via
      :func:`wrap_schema_prompt`.
    * Subprocess env is augmented with the OpenCode backend's API-key vars
      via :func:`build_opencode_env` so users only need to configure their
      LLM key once in ``AGENTS.toml``.
    """
    from codepilot.ai_support.planner_execution import (
        _emit_progress,
        _format_waiting_progress,
    )
    import os
    import sys

    provider_ref = config_ref or project_path
    cfg = cfg_loader(provider_ref)
    if cfg is None:
        raise RuntimeError(
            "OpenCode 兜底无法启动：未能加载 AGENTS.toml 配置。"
        )

    selection = select_opencode_backend(cfg)
    provider = resolve_cli_provider("opencode", provider_ref)
    exe = provider.find_executable()
    if not exe:
        raise RuntimeError(
            "当前无法使用 OpenCode 进行任务拆分，因为本机没有找到 `opencode` 命令。"
            "请先安装：https://opencode.ai 或 npm i -g opencode-ai。"
        )

    cmd = [str(exe), "run", wrap_schema_prompt(prompt, schema)]
    env = os.environ.copy()
    env.update(selection.env)

    process = None
    try:
        import threading
        import time as _time

        process = subprocess_module.Popen(
            cmd,
            stdin=subprocess_module.DEVNULL,
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.PIPE,
            env=env,
            **planner_process_group_kwargs_fn(),
        )
        _emit_progress(
            f"OpenCode 规划进程已启动 PID={process.pid} backend={selection.name}",
            get_progress_callback,
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        last_activity = [_time.monotonic()]

        def _read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                decoded_line = decode_planner_chunk(line)
                stdout_chunks.append(decoded_line)
                if decoded_line:
                    last_activity[0] = _time.monotonic()

        def _stream_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                decoded_line = decode_planner_chunk(line)
                stderr_chunks.append(decoded_line)
                stripped = decoded_line.rstrip()
                if not stripped:
                    continue
                last_activity[0] = _time.monotonic()
                sys.stderr.write(f"  [planner] {stripped}\n")
                sys.stderr.flush()
                _emit_progress(stripped, get_progress_callback)

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        started = _time.monotonic()
        hard_cap = max(timeout * 10, 1800)
        next_heartbeat_at = started + 10
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break
            now = _time.monotonic()
            elapsed = now - started
            idle = now - last_activity[0]

            if elapsed > hard_cap:
                kill_process_tree_fn(process.pid)
                process.wait(timeout=5)
                raise subprocess_module.TimeoutExpired(cmd, hard_cap)

            if idle > timeout:
                kill_process_tree_fn(process.pid)
                process.wait(timeout=5)
                raise subprocess_module.TimeoutExpired(cmd, timeout)

            if now >= next_heartbeat_at:
                _emit_progress(
                    _format_waiting_progress("OpenCode", elapsed=elapsed, idle=idle, timeout=timeout),
                    get_progress_callback,
                )
                next_heartbeat_at = now + 10

            _time.sleep(0.5)

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=2)
        result_returncode = process.returncode
        result_stdout = "".join(stdout_chunks)
        result_stderr = "".join(stderr_chunks)
    except subprocess_module.TimeoutExpired as exc:
        terminate_planner_process_fn(process)
        raise RuntimeError(
            f"OpenCode 在任务拆分阶段已连续 {timeout}s 没有任何输出，视为卡住并强制终止。"
            "可以稍后重试，或改用 claude / codex 作为规划器。"
        ) from exc
    except KeyboardInterrupt as exc:
        terminate_planner_process_fn(process)
        raise RuntimeError(
            "OpenCode 在任务拆分阶段被中断，已终止当前规划。可以稍后重试。"
        ) from exc
    except BaseException:
        terminate_planner_process_fn(process)
        raise

    if result_returncode != 0:
        hint = extract_error_hint("\n".join(part for part in (result_stderr, result_stdout) if part))
        suffix = f"原因：{hint}" if hint else "请检查 OpenCode 的 backend 配置或 API key 是否生效。"
        raise RuntimeError(f"OpenCode 没有成功完成任务拆分。{suffix}")

    output = result_stdout.strip()
    if not output:
        raise RuntimeError("OpenCode 任务拆分返回空内容")

    from codepilot.ai_support.result_parse import parse_structured_json_output

    return parse_structured_json_output(output, provider_name="OpenCode")
