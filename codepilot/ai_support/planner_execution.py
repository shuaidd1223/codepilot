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

    process = None
    try:
        import threading
        import time as _time

        process = subprocess_module.Popen(
            cmd,
            stdin=subprocess_module.DEVNULL,
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.PIPE,
            **planner_process_group_kwargs_fn(),
        )
        _emit_progress(f"{provider.name} 规划进程已启动 PID={process.pid}", get_progress_callback)

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        last_activity = [_time.monotonic()]

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

        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stderr_thread.start()

        def _read_stdout() -> None:
            assert process.stdout is not None
            stdout_chunks.append(decode_planner_chunk(process.stdout.read()))

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stdout_thread.start()

        started = _time.monotonic()
        hard_cap = max(timeout * 10, 1800)
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
                raise subprocess_module.TimeoutExpired(cmd, hard_cap)

            if idle > timeout:
                kill_process_tree_fn(process.pid)
                process.wait(timeout=5)
                raise subprocess_module.TimeoutExpired(cmd, timeout)

            if idle > 60 and not stall_warned:
                stall_warned = True
                msg = f"claude 已 {int(idle)}s 无输出（idle 超过 {timeout}s 会强制结束）..."
                sys.stderr.write(f"  [planner] {msg}\n")
                sys.stderr.flush()
                _emit_progress(msg, get_progress_callback)
            elif idle < 5 and stall_warned:
                stall_warned = False

            if now >= next_heartbeat_at:
                _emit_progress(
                    _format_waiting_progress(provider.name, elapsed=elapsed, idle=idle, timeout=timeout),
                    get_progress_callback,
                )
                next_heartbeat_at = now + 10

            _time.sleep(0.5)

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=2)

        result_stdout = "".join(stdout_chunks)
        result_stderr = "".join(stderr_chunks)
        result_returncode = process.returncode
    except subprocess_module.TimeoutExpired as exc:
        terminate_planner_process_fn(process)
        raise RuntimeError(
            f"{provider.name} 在任务拆分阶段已连续 {timeout}s 没有任何输出，视为卡住并强制终止。"
            "可以稍后重试，或改用 codex 作为规划器。"
        ) from exc
    except KeyboardInterrupt as exc:
        terminate_planner_process_fn(process)
        raise RuntimeError(
            f"{provider.name} 在任务拆分阶段被中断，已终止当前规划。可以稍后重试。"
        ) from exc
    except BaseException:
        terminate_planner_process_fn(process)
        raise

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

        process = None
        try:
            import threading
            import time as _time

            process = subprocess_module.Popen(
                cmd,
                stdin=subprocess_module.PIPE,
                stdout=subprocess_module.PIPE,
                stderr=subprocess_module.PIPE,
                **planner_process_group_kwargs_fn(),
            )
            if process.stdin:
                process.stdin.write(prompt.encode("utf-8"))
                process.stdin.close()
            _emit_progress(f"Codex 规划进程已启动 PID={process.pid}", get_progress_callback)

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            last_activity = [_time.monotonic()]

            def _read_codex_stdout() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    decoded_line = decode_planner_chunk(line)
                    stdout_chunks.append(decoded_line)
                    if decoded_line:
                        last_activity[0] = _time.monotonic()

            def _stream_codex_stderr() -> None:
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

            stdout_thread = threading.Thread(target=_read_codex_stdout, daemon=True)
            stderr_thread = threading.Thread(target=_stream_codex_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            started = _time.monotonic()
            hard_cap = max(timeout * 10, 1800)
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
                    raise subprocess_module.TimeoutExpired(cmd, hard_cap)

                if idle > timeout:
                    kill_process_tree_fn(process.pid)
                    process.wait(timeout=5)
                    raise subprocess_module.TimeoutExpired(cmd, timeout)

                if idle > 60 and not stall_warned:
                    stall_warned = True
                    msg = f"codex 已 {int(idle)}s 无输出（idle 超过 {timeout}s 会强制结束）..."
                    sys.stderr.write(f"  [planner] {msg}\n")
                    sys.stderr.flush()
                    _emit_progress(msg, get_progress_callback)
                elif idle < 5 and stall_warned:
                    stall_warned = False

                if now >= next_heartbeat_at:
                    _emit_progress(
                        _format_waiting_progress("Codex", elapsed=elapsed, idle=idle, timeout=timeout),
                        get_progress_callback,
                    )
                    next_heartbeat_at = now + 10

                _time.sleep(0.5)

            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=2)
            codex_returncode = process.returncode
            codex_stdout = "".join(stdout_chunks)
            codex_stderr = "".join(stderr_chunks)
        except subprocess_module.TimeoutExpired as exc:
            terminate_planner_process_fn(process)
            raise RuntimeError(
                f"Codex 在任务拆分阶段已连续 {timeout}s 没有任何输出，视为卡住并强制终止。"
                "可以稍后重试，或改用 claude 作为规划器。"
            ) from exc
        except KeyboardInterrupt as exc:
            terminate_planner_process_fn(process)
            raise RuntimeError("Codex 在任务拆分阶段被中断，已终止当前规划。可以稍后重试。") from exc
        except BaseException:
            terminate_planner_process_fn(process)
            raise

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
