"""Helpers for planner CLI execution.

These functions hold the heavy subprocess orchestration so
``codepilot.ai_support.service`` can remain a compatibility facade while tests
still monkeypatch service-level wrappers.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


def _emit_progress(message: str, get_progress_callback: Callable[[], Callable[[str], None] | None]) -> None:
    callback = get_progress_callback()
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        pass


def _format_waiting_progress(provider_name: str, *, elapsed: float, idle: float, timeout: int) -> str:
    return (
        f"{provider_name} 正在规划：已等待 {int(elapsed)}s，"
        f"最近 {int(idle)}s 无新输出；超过 {timeout}s 无输出会自动终止。"
    )


def kill_process_tree(pid: int) -> None:
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


def planner_process_group_kwargs() -> dict[str, Any]:
    """Return subprocess kwargs that isolate planner children safely."""
    from codepilot.core.runtime import no_window_kwargs

    return no_window_kwargs(new_process_group=True)


def terminate_planner_process(process: Any, *, kill_process_tree_fn: Callable[[int], None]) -> None:
    """Best-effort cleanup for planner subprocesses."""
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if pid:
        try:
            kill_process_tree_fn(int(pid))
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass


@dataclass(frozen=True)
class _PlannerSubprocessSpec:
    """Inputs for the shared planner subprocess watchdog.

    Each ``run_*_schema_prompt`` wrapper builds one of these, hands it to
    :func:`_run_planner_subprocess`, then post-processes the returned
    ``(stdout, stderr, returncode)`` tuple in family-specific ways
    (e.g. Codex reads the result from a file path it asked the CLI to
    write, while Claude / OpenCode read it directly from stdout).
    """

    cmd: list[str]
    provider_name: str
    family_label: str
    timeout: int
    env: dict[str, str] | None = None
    stdin_data: bytes | None = None
    stdout_strategy: str = "stream"  # "stream" | "read_at_end"
    suggested_fallback: str = ""
    stdout_chunk_callback: Callable[[str], None] | None = None


def _iter_stdout_chunks(stream: Any):
    read = getattr(stream, "read", None)
    if callable(read):
        read_supported = True
        while True:
            try:
                chunk = read(4096)
            except TypeError:
                read_supported = False
                break
            if not chunk:
                return
            yield chunk
        if read_supported:
            return
    for chunk in stream:
        yield chunk


def _emit_stdout_chunk(message: str, callback: Callable[[str], None] | None) -> None:
    if callback is None or not message:
        return
    try:
        callback(message)
    except Exception:
        pass


def _run_planner_subprocess(
    spec: _PlannerSubprocessSpec,
    *,
    subprocess_module: Any,
    planner_process_group_kwargs_fn: Callable[[], dict[str, Any]],
    decode_planner_chunk: Callable[[str | bytes | None], str],
    kill_process_tree_fn: Callable[[int], None],
    terminate_planner_process_fn: Callable[[Any], None],
    get_progress_callback: Callable[[], Callable[[str], None] | None],
) -> tuple[str, str, int]:
    """Run a planner CLI subprocess with idle/hard timeout, heartbeat, cleanup.

    Shared by ``run_claude_schema_prompt`` / ``run_codex_schema_prompt`` /
    ``run_opencode_schema_prompt`` so the wait-loop bookkeeping (idle
    threshold, heartbeat cadence, ``KeyboardInterrupt`` handling, kill on
    timeout) lives in one place.

    Returns ``(stdout, stderr, returncode)``. Raises ``RuntimeError`` on
    idle/hard timeout (with a CLI-friendly suggestion) and on
    ``KeyboardInterrupt``.
    """
    import threading
    import time as _time

    use_stdin_pipe = spec.stdin_data is not None
    stdin_arg = subprocess_module.PIPE if use_stdin_pipe else subprocess_module.DEVNULL

    popen_kwargs: dict[str, Any] = dict(planner_process_group_kwargs_fn())
    if spec.env is not None:
        popen_kwargs["env"] = spec.env

    process = None
    try:
        process = subprocess_module.Popen(
            spec.cmd,
            stdin=stdin_arg,
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.PIPE,
            **popen_kwargs,
        )
        if use_stdin_pipe and process.stdin is not None:
            process.stdin.write(spec.stdin_data)
            process.stdin.close()

        _emit_progress(
            f"{spec.provider_name} 规划进程已启动 PID={process.pid}",
            get_progress_callback,
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        last_activity = [_time.monotonic()]

        if spec.stdout_strategy == "read_at_end":
            def _read_stdout() -> None:
                assert process.stdout is not None
                stdout_chunks.append(decode_planner_chunk(process.stdout.read()))
        else:
            def _read_stdout() -> None:
                assert process.stdout is not None
                stdout_iter = (
                    _iter_stdout_chunks(process.stdout)
                    if spec.stdout_chunk_callback is not None
                    else process.stdout
                )
                for line in stdout_iter:
                    decoded_line = decode_planner_chunk(line)
                    stdout_chunks.append(decoded_line)
                    if decoded_line:
                        last_activity[0] = _time.monotonic()
                        _emit_stdout_chunk(decoded_line, spec.stdout_chunk_callback)

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
        hard_cap = max(spec.timeout * 10, 1800)
        stall_warned = False
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
                raise subprocess_module.TimeoutExpired(spec.cmd, hard_cap)

            if idle > spec.timeout:
                kill_process_tree_fn(process.pid)
                process.wait(timeout=5)
                raise subprocess_module.TimeoutExpired(spec.cmd, spec.timeout)

            if idle > 60 and not stall_warned:
                stall_warned = True
                msg = (
                    f"{spec.family_label} 已 {int(idle)}s 无输出"
                    f"（idle 超过 {spec.timeout}s 会强制结束）..."
                )
                sys.stderr.write(f"  [planner] {msg}\n")
                sys.stderr.flush()
                _emit_progress(msg, get_progress_callback)
            elif idle < 5 and stall_warned:
                stall_warned = False

            if now >= next_heartbeat_at:
                _emit_progress(
                    _format_waiting_progress(
                        spec.provider_name, elapsed=elapsed, idle=idle, timeout=spec.timeout
                    ),
                    get_progress_callback,
                )
                next_heartbeat_at = now + 10

            _time.sleep(0.5)

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=2)
        return "".join(stdout_chunks), "".join(stderr_chunks), process.returncode
    except subprocess_module.TimeoutExpired as exc:
        terminate_planner_process_fn(process)
        suffix = (
            f"可以稍后重试，或改用 {spec.suggested_fallback} 作为规划器。"
            if spec.suggested_fallback
            else "可以稍后重试。"
        )
        raise RuntimeError(
            f"{spec.provider_name} 在任务拆分阶段已连续 {spec.timeout}s "
            f"没有任何输出，视为卡住并强制终止。{suffix}"
        ) from exc
    except KeyboardInterrupt as exc:
        terminate_planner_process_fn(process)
        raise RuntimeError(
            f"{spec.provider_name} 在任务拆分阶段被中断，已终止当前规划。可以稍后重试。"
        ) from exc
    except BaseException:
        terminate_planner_process_fn(process)
        raise


def run_claude_schema_prompt(
    prompt: str,
    schema: dict,
    *,
    planner: str = "codex",
    project_path: str = "",
    config_ref: str | Path | None = None,
    timeout: int = 240,
    normalize_agent_name: Callable[[str], str],
    resolve_cli_provider: Callable[[str, str | Path | None], Any],
    get_node_modules_path: Callable[[], str],
    subprocess_module: Any,
    planner_process_group_kwargs_fn: Callable[[], dict[str, Any]],
    decode_planner_chunk: Callable[[str | bytes | None], str],
    kill_process_tree_fn: Callable[[int], None],
    terminate_planner_process_fn: Callable[[Any], None],
    extract_error_hint: Callable[[str], str],
    get_progress_callback: Callable[[], Callable[[str], None] | None],
    stream_callback: Callable[[str], None] | None = None,
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
        cli_js = Path(get_node_modules_path()) / "@anthropic-ai" / "claude-code" / "cli.js"
        if not cli_js.exists():
            raise RuntimeError(
                "当前无法使用 Claude Code (Node) 进行任务拆分，因为没有找到全局安装的 "
                "`@anthropic-ai/claude-code`。请先执行: npm install -g @anthropic-ai/claude-code"
            )
        cmd.append(str(cli_js))

    cmd.extend(
        [
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False),
            "--dangerously-skip-permissions",
        ]
    )
    if model_alias:
        cmd.extend(["--model", model_alias])
    cmd.extend(["-p", prompt])

    spec = _PlannerSubprocessSpec(
        cmd=cmd,
        provider_name=provider.name,
        family_label="claude",
        timeout=timeout,
        stdout_strategy="stream" if stream_callback is not None else "read_at_end",
        suggested_fallback="codex",
        stdout_chunk_callback=stream_callback,
    )
    result_stdout, result_stderr, result_returncode = _run_planner_subprocess(
        spec,
        subprocess_module=subprocess_module,
        planner_process_group_kwargs_fn=planner_process_group_kwargs_fn,
        decode_planner_chunk=decode_planner_chunk,
        kill_process_tree_fn=kill_process_tree_fn,
        terminate_planner_process_fn=terminate_planner_process_fn,
        get_progress_callback=get_progress_callback,
    )

    if result_returncode != 0:
        hint = extract_error_hint("\n".join(part for part in (result_stderr, result_stdout) if part))
        suffix = f"原因：{hint}" if hint else "请检查 Claude CLI 当前是否可用。"
        raise RuntimeError(f"{provider.name} 没有成功完成任务拆分。{suffix}")

    output = result_stdout.strip()
    if not output:
        raise RuntimeError("Claude 任务拆分返回空内容")

    from codepilot.ai_support.result_parse import parse_structured_json_output

    return parse_structured_json_output(output, provider_name=provider.name)


def run_codex_schema_prompt(
    prompt: str,
    schema: dict,
    *,
    project_path: str = "",
    config_ref: str | Path | None = None,
    timeout: int = 240,
    check_provider_availability: Callable[[str, str | Path | None], tuple[bool, str]],
    resolve_cli_provider: Callable[[str, str | Path | None], Any],
    subprocess_module: Any,
    planner_process_group_kwargs_fn: Callable[[], dict[str, Any]],
    decode_planner_chunk: Callable[[str | bytes | None], str],
    kill_process_tree_fn: Callable[[int], None],
    terminate_planner_process_fn: Callable[[Any], None],
    extract_error_hint: Callable[[str], str],
    get_progress_callback: Callable[[], Callable[[str], None] | None],
    stream_callback: Callable[[str], None] | None = None,
) -> dict:
    """Use Codex CLI with a JSON schema output contract."""
    provider_ref = config_ref or project_path
    available, message = check_provider_availability("codex", project_path=provider_ref)
    if not available:
        raise RuntimeError(message)
    exe = resolve_cli_provider("codex", provider_ref).find_executable()
    if not exe:
        raise RuntimeError("当前无法使用 Codex 进行任务拆分，因为本机没有找到 `codex` 命令。")

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

        spec = _PlannerSubprocessSpec(
            cmd=cmd,
            provider_name="Codex",
            family_label="codex",
            timeout=timeout,
            stdin_data=prompt.encode("utf-8"),
            stdout_strategy="stream",
            suggested_fallback="claude",
            stdout_chunk_callback=stream_callback,
        )
        codex_stdout, codex_stderr, codex_returncode = _run_planner_subprocess(
            spec,
            subprocess_module=subprocess_module,
            planner_process_group_kwargs_fn=planner_process_group_kwargs_fn,
            decode_planner_chunk=decode_planner_chunk,
            kill_process_tree_fn=kill_process_tree_fn,
            terminate_planner_process_fn=terminate_planner_process_fn,
            get_progress_callback=get_progress_callback,
        )

        if codex_returncode != 0:
            hint = extract_error_hint("\n".join(part for part in (codex_stderr, codex_stdout) if part))
            suffix = f"原因：{hint}" if hint else "请检查 Codex CLI 当前是否可用。"
            raise RuntimeError(f"Codex 没有成功完成任务拆分。{suffix}")

        output = output_path.read_text(encoding="utf-8", errors="replace").strip() if output_path.exists() else ""
        if not output:
            output = codex_stdout.strip()
    if not output:
        raise RuntimeError("Codex 没有返回任务拆分结果，暂时无法继续自动规划。")

    from codepilot.ai_support.result_parse import parse_structured_json_output

    return parse_structured_json_output(output, provider_name="Codex")


def run_opencode_schema_prompt(
    prompt: str,
    schema: dict,
    *,
    project_path: str = "",
    config_ref: str | Path | None = None,
    timeout: int = 240,
    cfg_loader: Callable[[Any], Any],
    resolve_cli_provider: Callable[[str, Any], Any],
    subprocess_module: Any,
    planner_process_group_kwargs_fn: Callable[[], dict[str, Any]],
    decode_planner_chunk: Callable[[str | bytes | None], str],
    kill_process_tree_fn: Callable[[int], None],
    terminate_planner_process_fn: Callable[[Any], None],
    extract_error_hint: Callable[[str], str],
    get_progress_callback: Callable[[], Callable[[str], None] | None],
    stream_callback: Callable[[str], None] | None = None,
) -> dict:
    """Use OpenCode CLI to produce schema-constrained JSON output.

    Shares :func:`_run_planner_subprocess` with the Claude / Codex runners.
    OpenCode is provider-agnostic; the env-bridge in ``opencode_runtime``
    selects the LLM backend (anthropic / deepseek / openai-compat) from
    configured providers and injects the matching API-key vars before
    spawning ``opencode run``.
    """
    from codepilot.ai_support.opencode_runtime import (
        select_opencode_backend,
        wrap_schema_prompt,
    )
    from codepilot.ai_support.result_parse import parse_structured_json_output

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

    spec = _PlannerSubprocessSpec(
        cmd=cmd,
        provider_name=f"OpenCode ({selection.name})",
        family_label="opencode",
        timeout=timeout,
        env=env,
        stdout_strategy="stream",
        suggested_fallback="claude / codex",
        stdout_chunk_callback=stream_callback,
    )
    result_stdout, result_stderr, result_returncode = _run_planner_subprocess(
        spec,
        subprocess_module=subprocess_module,
        planner_process_group_kwargs_fn=planner_process_group_kwargs_fn,
        decode_planner_chunk=decode_planner_chunk,
        kill_process_tree_fn=kill_process_tree_fn,
        terminate_planner_process_fn=terminate_planner_process_fn,
        get_progress_callback=get_progress_callback,
    )

    if result_returncode != 0:
        hint = extract_error_hint("\n".join(part for part in (result_stderr, result_stdout) if part))
        suffix = f"原因：{hint}" if hint else "请检查 OpenCode 的 backend 配置或 API key 是否生效。"
        raise RuntimeError(f"OpenCode 没有成功完成任务拆分。{suffix}")

    output = result_stdout.strip()
    if not output:
        raise RuntimeError("OpenCode 任务拆分返回空内容")

    return parse_structured_json_output(output, provider_name="OpenCode")
