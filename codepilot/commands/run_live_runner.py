"""Live subprocess runner for task execution.

Split out from ``run_shell.py`` so the real-time execution boundary stays
isolated from shell selection and plain subprocess helpers.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from codepilot.core.runtime import (
    HEARTBEAT_INTERVAL_SECONDS,
    get_stop_request,
    stop_process_tree,
    tail_text,
    update_task_runtime,
)
from codepilot.core.text_decode import decode_subprocess_text


class TaskCancelled(RuntimeError):
    """Raised when a running task is explicitly stopped."""


def _should_show_line(line: str) -> bool:
    """Filter: only show concise progress lines, suppress raw code/diff output."""
    s = line.strip()
    if not s:
        return False
    # Always show these patterns
    _show_patterns = (
        # Agent lifecycle
        "session id:", "model:", "provider:", "workdir:", "sandbox:",
        "tokens used", "approval:",
        # Phase markers
        "user", "codex", "claude", "assistant",
        # File operations
        "Created ", "Modified ", "Deleted ", "Renamed ",
        "created ", "modified ", "deleted ", "renamed ",
        "Writing ", "Reading ", "Wrote ", "Read ",
        # Commands being run
        "Running ", "Executing ", "$ ", "running ",
        # Test results
        "passed", "failed", "PASSED", "FAILED", "VERDICT",
        "pytest", "test_",
        # Git
        "commit ", "branch ", "merge ",
        # Errors
        "Error", "error:", "ERROR", "Warning", "WARNING",
        # Summary lines
        "Summary", "Changed Files", "Validation",
    )
    for pat in _show_patterns:
        if pat in s:
            return True
    # Show lines that look like file paths being modified
    if ("/" in s or "\\" in s) and any(ext in s for ext in (".py", ".js", ".ts", ".vue", ".html", ".css", ".json", ".toml")):
        # But not if it's a code line (starts with common code chars)
        if not s.startswith(("import ", "from ", "def ", "class ", "    ", "\t", "return ", "if ", "else", "#", "//", "/*")):
            return True
    # Suppress everything else (raw code, diffs, etc.)
    return False


def _summarize_output(output: str) -> list[str]:
    """Extract a concise summary: modified files and line counts."""
    lines = (output or "").splitlines()
    summary = []
    files_seen = set()
    for line in lines:
        s = line.strip()
        # Look for file modification patterns
        for prefix in ("Created ", "Modified ", "Deleted ", "Renamed ",
                       "created ", "modified ", "deleted ", "renamed ",
                       "Wrote ", "Writing "):
            if s.startswith(prefix):
                summary.append(s[:120])
                break
        # Look for "Changed Files" section in codex output
        if s.startswith(("- ", "* ")) and any(ext in s for ext in (".py", ".js", ".ts", ".vue", ".html")):
            if s not in files_seen:
                files_seen.add(s)
                summary.append(s[:120])
    # If no file-level info found, show last few meaningful lines
    if not summary:
        meaningful = [line.strip() for line in lines if line.strip() and not line.strip().startswith(("import ", "from ", "def ", "class ", "    "))]
        summary = meaningful[-5:]
    return summary[:15]


_LOG_ARG_REDACT_CHARS = 400
_PROMPT_ARG_MARKERS = (
    "【任务目标】",
    "【验收标准",
    "【项目约定",
    "[Requirements]",
    "AGENTS.md",
    "TDD Mode",
    "Task file",
)


def _join_command_parts_for_markdown(parts: list[str]) -> str:
    parts = [str(part) for part in (parts or [])]
    if not parts:
        return ""
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def _format_command_for_markdown(cmd: list[str]) -> str:
    parts = [str(part) for part in (cmd or [])]
    return _join_command_parts_for_markdown(parts)


def _command_arg_redaction_reason(text: str) -> str:
    value = str(text or "")
    if any(marker in value for marker in _PROMPT_ARG_MARKERS):
        return "prompt"
    if "\n" in value or "\r" in value:
        return "prompt" if len(value) > 120 else "multiline"
    if len(value) > _LOG_ARG_REDACT_CHARS:
        return "long"
    return ""


def _format_command_for_log(cmd: list[str]) -> tuple[str, list[dict[str, int | str]]]:
    display_parts: list[str] = []
    redactions: list[dict[str, int | str]] = []
    for idx, part in enumerate(cmd or []):
        text = str(part)
        reason = _command_arg_redaction_reason(text)
        if reason:
            label = "prompt" if reason == "prompt" else "long"
            display_parts.append(f"[omitted {label} argument: {len(text)} chars]")
            redactions.append({"index": idx, "chars": len(text), "reason": reason})
        else:
            display_parts.append(text)
    return _join_command_parts_for_markdown(display_parts), redactions


def _write_markdown_preamble(
    handle,
    *,
    task_id: int,
    phase: str,
    cmd: list[str],
    cwd: Optional[Path],
    timeout: int,
    input_text: Optional[str] = None,
) -> int:
    started = datetime.now().isoformat(timespec="seconds")
    cwd_text = str(cwd) if cwd else str(Path.cwd())
    cmd_text, redactions = _format_command_for_log(cmd)
    meta = {
        "kind": "live_command",
        "version": 1,
        "task_id": int(task_id),
        "phase": str(phase),
        "cwd": cwd_text,
        "timeout_seconds": int(timeout),
        "stdin_chars": len(input_text or ""),
        "command_redactions": redactions,
    }
    input_lines = []
    if redactions:
        input_lines.append(f"- command_args: `{len(redactions)} omitted`")
    if input_text is not None:
        input_lines.append(f"- stdin_chars: `{len(input_text)}`")

    text = (
        f"# Task #{task_id} · {phase}\n\n"
        f"CODEPILOT_LOG_META: {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"- started_at: `{started}`\n"
        f"- phase: `{phase}`\n"
        f"- cwd: `{cwd_text}`\n"
        f"- timeout_seconds: `{int(timeout)}`\n\n"
        "## Command\n\n"
        "```shell\n"
        f"{cmd_text}\n"
        "```\n\n"
    )
    if input_lines:
        text += "## Input\n\n" + "\n".join(input_lines) + "\n\n"
    text += "## Live Output\n\n"
    handle.write(text)
    handle.flush()
    return len(text.encode("utf-8", errors="replace"))


def _write_markdown_footer(
    handle,
    *,
    status: str,
    exit_code: int | None,
    detail: str = "",
    summary: list[str] | None = None,
) -> None:
    finished = datetime.now().isoformat(timespec="seconds")
    lines = [
        "",
        "## Result",
        "",
        f"- status: `{status}`",
        f"- exit_code: `{exit_code if exit_code is not None else '-'}`",
        f"- finished_at: `{finished}`",
    ]
    if detail:
        lines.append(f"- detail: `{detail}`")
    if summary:
        lines.extend(["", "### Summary"])
        for item in summary[:15]:
            clean = (item or "").strip()
            if clean:
                lines.append(f"- {clean}")
    lines.append("")
    handle.write("\n".join(lines))
    handle.flush()


def _format_status_console_line(line: str) -> str:
    from rich.markup import escape as _rich_escape

    s = (line or "").strip()
    if not s:
        return ""
    low = s.lower()
    show = _rich_escape(s[:220])

    if s in {"user", "codex", "claude", "assistant"}:
        color = {
            "user": "bright_white",
            "codex": "bright_cyan",
            "claude": "bright_yellow",
            "assistant": "bright_magenta",
        }.get(s, "white")
        return f"[bold {color}]▌ {s.upper()}[/]"
    if s == "exec":
        return "[bold cyan]▶ EXEC[/]"
    if low.startswith("succeeded in"):
        return f"[bold green]✓ {show}[/]"
    if "failed in" in low or low.startswith("failed:") or low.startswith("error:") or "traceback" in low:
        return f"[bold red]✗ {show}[/]"
    if s.startswith("diff --git"):
        return f"[bold magenta]Δ {show}[/]"
    if s.startswith("@@ "):
        return f"[bright_blue]{show}[/]"
    if s.startswith(("Created ", "Modified ", "Deleted ", "Renamed ", "Wrote ", "Writing ")):
        return f"[bold yellow]{show}[/]"
    if s.startswith(("## ", "### ", "# ")):
        return f"[bold bright_white]{show}[/]"
    if re.match(r"^\s*(?:[+-]\s*)?def\s+[A-Za-z_]\w*\s*\(", s):
        return f"[bold bright_yellow]{show}[/]"
    return f"[dim]{show}[/]"


@dataclass
class _LiveRuntimeDeps:
    get_stop_request: Callable[[int], tuple[bool, str]]
    stop_process_tree: Callable[[int], bool]
    update_task_runtime: Callable[..., Any]
    status_console: Any | None = None


@dataclass
class _LiveRunStatus:
    state: str = "running"
    detail: str = ""
    exit_code: int | None = None


def _resolve_live_runtime_deps() -> _LiveRuntimeDeps:
    """Late-bind runtime hooks so tests can monkeypatch `run` shell exports."""
    try:
        from codepilot.commands import run as _rc

        return _LiveRuntimeDeps(
            get_stop_request=_rc.get_stop_request,
            stop_process_tree=_rc.stop_process_tree,
            update_task_runtime=_rc.update_task_runtime,
            status_console=getattr(_rc, "STATUS_CONSOLE", None),
        )
    except Exception:
        return _LiveRuntimeDeps(
            get_stop_request=get_stop_request,
            stop_process_tree=stop_process_tree,
            update_task_runtime=update_task_runtime,
            status_console=None,
        )


def _should_use_pty_for_live_stream() -> bool:
    # 可选 PTY：某些 CLI（codex/claude）在非 TTY 下会切块缓冲，用伪终端可以让它按行刷。
    # 仅在 Unix 且显式开启环境变量 CODEPILOT_USE_PTY=1 时启用，Windows 默认保持 Popen 管道。
    return (
        os.name != "nt"
        and os.environ.get("CODEPILOT_USE_PTY", "").strip() in {"1", "true", "yes"}
    )


def _resolve_stream_chunk_size() -> int:
    raw_chunk_size = 64
    raw_chunk_env = os.environ.get("CODEPILOT_LOG_STREAM_CHUNK_CHARS", "").strip()
    if raw_chunk_env:
        try:
            raw_chunk_size = max(8, min(256, int(raw_chunk_env)))
        except ValueError:
            raw_chunk_size = 64
    return raw_chunk_size


def _build_live_popen_kwargs(
    *,
    cwd: Optional[Path],
    input_text: Optional[str],
    use_pty: bool,
) -> tuple[dict[str, Any], Optional[int], Optional[int]]:
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.PIPE if input_text is not None else None,
        "bufsize": 0,
    }
    pty_master_fd: Optional[int] = None
    pty_slave_fd: Optional[int] = None
    if use_pty:
        import pty  # Unix-only

        pty_master_fd, pty_slave_fd = pty.openpty()
        popen_kwargs["stdout"] = pty_slave_fd
        popen_kwargs["stderr"] = pty_slave_fd
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["stdout"] = subprocess.PIPE
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        else:
            # Prevent console pop-ups when the parent (e.g. the detached
            # webui service) has no console of its own.
            from codepilot.core.runtime import no_window_kwargs

            popen_kwargs.update(no_window_kwargs())
    # Ensure subprocesses use UTF-8 I/O on all platforms; Windows tools may
    # otherwise emit UTF-16 output that causes garbled log text.
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    popen_kwargs["env"] = env
    return popen_kwargs, pty_master_fd, pty_slave_fd


class _LiveOutputProcessor:
    """Handle streaming output conversion, persistence, and progress events."""

    def __init__(
        self,
        *,
        task_id: int,
        phase: str,
        handle,
        raw_chunk_size: int,
        status_console: Any | None = None,
    ) -> None:
        self.task_id = int(task_id)
        self.phase = str(phase)
        self.handle = handle
        self.raw_chunk_size = int(raw_chunk_size)
        self.status_console = status_console
        # Stream event offsets are byte offsets (start from 0),
        # independent of markdown preamble bytes in the log file.
        self.emitted_log_bytes = 0
        self.recent_lines: list[str] = []
        self.recent_lock = threading.Lock()
        # Monotonic timestamp of the most recent byte received from the
        # subprocess — the silence detector compares this with
        # ``time.monotonic()`` to decide whether the agent has gone quiet.
        self.last_output_monotonic = time.monotonic()
        self.footer_written = False

    def _emit_log_stream(self, raw: str, start_offset: int) -> None:
        from codepilot.core import progress_bus

        cursor = start_offset
        idx = 0
        while idx < len(raw):
            piece = raw[idx: idx + self.raw_chunk_size]
            idx += len(piece)
            piece_bytes = len(piece.encode("utf-8", errors="replace"))
            end_offset = cursor + piece_bytes
            try:
                progress_bus.emit(
                    task_id=self.task_id,
                    stage=self.phase,
                    level="info",
                    message="",
                    extra={
                        "source": "subprocess",
                        "task_log_stream": True,
                        "task_log_chunk": piece,
                        "task_log_start": cursor,
                        "task_log_end": end_offset,
                    },
                )
            except Exception:
                pass
            cursor = end_offset

    def _emit_progress_line(self, stripped: str) -> None:
        from codepilot.core import progress_bus

        if not _should_show_line(stripped):
            return
        try:
            styled = _format_status_console_line(stripped)
            if styled and self.status_console is not None:
                self.status_console.print(f"    {styled}")
        except Exception:
            pass
        try:
            progress_bus.emit(
                task_id=self.task_id,
                stage=self.phase,
                level="info",
                message=stripped[:200],
                extra={"source": "subprocess"},
            )
        except Exception:
            pass

    def emit(self, raw: str) -> None:
        if not raw:
            return
        # Any inbound byte resets the silence clock — even whitespace
        # counts as "agent still responsive".
        self.last_output_monotonic = time.monotonic()
        # Keep subprocess bytes auditable in the console log. The stable
        # structure lives in the CodePilot preamble/meta and the renderer
        # consumes that before falling back to transcript heuristics.
        stream_start = self.emitted_log_bytes
        raw_bytes = raw.encode("utf-8", errors="replace")
        self.emitted_log_bytes = stream_start + len(raw_bytes)
        try:
            self.handle.write(raw)
            self.handle.flush()
        except Exception:
            pass
        self._emit_log_stream(raw, stream_start)

        stripped = raw.rstrip()
        if stripped:
            # Only surface concise progress lines (the CLI tools emit
            # a lot of raw code); terminal summary events stay filtered.
            # Raw log bytes are pushed separately via task_log_stream.
            self._emit_progress_line(stripped)
        with self.recent_lock:
            self.recent_lines.append(raw)
            if len(self.recent_lines) > 200:
                del self.recent_lines[:-200]

    def emit_run_status(self, *, elapsed_seconds: int, silent_seconds: int) -> None:
        """Send a lightweight run status event via progress_bus for the
        frontend Run Status bar — does NOT write to the log file.

        The frontend uses this to display an animated "agent thinking"
        indicator instead of noisy heartbeat log lines.
        """
        try:
            from codepilot.core import progress_bus

            progress_bus.emit(
                task_id=self.task_id,
                stage=self.phase,
                level="info",
                message="",
                extra={
                    "source": "task_run_status",
                    "elapsed_seconds": int(elapsed_seconds),
                    "silent_seconds": int(silent_seconds),
                },
            )
        except Exception:
            pass

    def seconds_since_last_output(self) -> float:
        return time.monotonic() - self.last_output_monotonic

    def preview(self) -> str:
        with self.recent_lock:
            return "".join(self.recent_lines[-20:]).strip()

    def summary(self) -> list[str]:
        with self.recent_lock:
            return _summarize_output("".join(self.recent_lines))

    def finalize(self, *, status: str, exit_code: int | None, detail: str = "") -> None:
        if self.footer_written:
            return
        _write_markdown_footer(
            self.handle,
            status=str(status),
            exit_code=exit_code,
            detail=str(detail or ""),
            summary=self.summary(),
        )
        self.footer_written = True


def _pump_process_stdout(
    process: subprocess.Popen,
    *,
    use_pty: bool,
    pty_master_fd: Optional[int],
    emit: Callable[[str], None],
) -> None:
    if use_pty and pty_master_fd is not None:
        buf = b""
        while True:
            try:
                chunk = os.read(pty_master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                emit(decode_subprocess_text(line) + "\n")
        if buf:
            emit(decode_subprocess_text(buf))
        try:
            os.close(pty_master_fd)
        except Exception:
            pass
        return
    assert process.stdout is not None
    for raw in process.stdout:
        emit(decode_subprocess_text(raw))


def _stop_reader_and_process(
    process: subprocess.Popen,
    *,
    reader: threading.Thread,
    stop_process_tree_fn: Callable[[int], bool],
    reader_timeout: float = 2.0,
) -> None:
    stop_process_tree_fn(process.pid)
    reader.join(timeout=reader_timeout)


def _poll_live_process(
    *,
    cmd: list[str],
    process: subprocess.Popen,
    reader: threading.Thread,
    task_id: int,
    phase: str,
    log_path: Path,
    timeout: int,
    silence_timeout_seconds: int,
    deps: _LiveRuntimeDeps,
    output: _LiveOutputProcessor,
    status: _LiveRunStatus,
) -> _LiveRunStatus:
    started = time.monotonic()
    last_heartbeat = 0.0
    last_idle_log_heartbeat = started

    while True:
        requested, reason = deps.get_stop_request(task_id)
        if requested:
            _stop_reader_and_process(process, reader=reader, stop_process_tree_fn=deps.stop_process_tree)
            status.state = "cancelled"
            status.detail = reason or f"任务 #{task_id} 已停止"
            deps.update_task_runtime(
                task_id,
                phase=phase,
                pid=process.pid,
                log_path=log_path,
                last_output=tail_text(log_path),
            )
            raise TaskCancelled(reason or f"任务 #{task_id} 已停止")

        if time.monotonic() - started > timeout:
            _stop_reader_and_process(process, reader=reader, stop_process_tree_fn=deps.stop_process_tree)
            status.state = "timeout"
            status.detail = f"超过超时阈值 {timeout}s"
            raise subprocess.TimeoutExpired(cmd, timeout)

        # Agent silence detector — user opt-in via config. Guards
        # against the "subprocess alive but wedged" case that the
        # wall-time timeout takes minutes to catch.
        if silence_timeout_seconds > 0:
            silent_for = output.seconds_since_last_output()
            if silent_for > silence_timeout_seconds:
                _stop_reader_and_process(process, reader=reader, stop_process_tree_fn=deps.stop_process_tree)
                status.state = "timeout_silence"
                status.detail = f"连续 {int(silent_for)}s 无输出（阈值 {silence_timeout_seconds}s）"
                raise subprocess.TimeoutExpired(
                    cmd,
                    silence_timeout_seconds,
                    output=(
                        f"子进程连续 {int(silent_for)}s 无输出（阈值 {silence_timeout_seconds}s），"
                        "已按 agent_silence_timeout_seconds 策略终止。"
                    ),
                )

        exit_code = process.poll()
        now = time.monotonic()
        if exit_code is None:
            silent_for = output.seconds_since_last_output()
            if (
                now - last_idle_log_heartbeat >= HEARTBEAT_INTERVAL_SECONDS
                and silent_for >= HEARTBEAT_INTERVAL_SECONDS
            ):
                output.emit_run_status(
                    elapsed_seconds=int(now - started),
                    silent_seconds=int(silent_for),
                )
                last_idle_log_heartbeat = now

        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            deps.update_task_runtime(
                task_id,
                phase=phase,
                pid=process.pid if exit_code is None else None,
                log_path=log_path,
                last_output=output.preview(),
            )
            last_heartbeat = now

        if exit_code is not None:
            reader.join(timeout=5)
            status.state = "ok" if exit_code == 0 else "failed"
            status.exit_code = exit_code
            deps.update_task_runtime(
                task_id,
                phase=phase,
                pid=None,
                log_path=log_path,
                last_output=tail_text(log_path),
            )
            return status

        time.sleep(0.1)


def _run_command_live(
    cmd: list[str],
    *,
    task_id: int,
    phase: str,
    log_path: Path,
    cwd: Optional[Path] = None,
    timeout: int = 3600,
    input_text: Optional[str] = None,
    silence_timeout_seconds: int = 0,
) -> tuple[int, str]:
    """Run a long-lived command while streaming output, updating heartbeat, and honoring stop requests.

    ``silence_timeout_seconds`` > 0 enables an "assumed dead" detector: if no
    new bytes have been emitted by the subprocess for that many seconds, the
    whole process tree gets killed and :class:`subprocess.TimeoutExpired` is
    raised. 0 keeps the old behaviour (only wall-time ``timeout`` applies).
    """
    deps = _resolve_live_runtime_deps()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    use_pty = _should_use_pty_for_live_stream()
    with log_path.open("w", encoding="utf-8", errors="replace") as handle:
        raw_chunk_size = _resolve_stream_chunk_size()
        popen_kwargs, pty_master_fd, pty_slave_fd = _build_live_popen_kwargs(
            cwd=cwd,
            input_text=input_text,
            use_pty=use_pty,
        )

        _write_markdown_preamble(
            handle,
            task_id=task_id,
            phase=phase,
            cmd=cmd,
            cwd=cwd,
            timeout=timeout,
            input_text=input_text,
        )
        output = _LiveOutputProcessor(
            task_id=task_id,
            phase=phase,
            handle=handle,
            raw_chunk_size=raw_chunk_size,
            status_console=deps.status_console,
        )

        process = subprocess.Popen(cmd, **popen_kwargs)
        if use_pty and pty_slave_fd is not None:
            # 关掉父进程这端的 slave，让子进程退出时 read 能收到 EOF
            try:
                os.close(pty_slave_fd)
            except Exception:
                pass
        if input_text is not None and process.stdin:
            process.stdin.write(input_text.encode("utf-8"))
            process.stdin.close()

        deps.update_task_runtime(task_id, phase=phase, pid=process.pid, log_path=log_path, last_output="")
        run_status = _LiveRunStatus()
        reader = threading.Thread(
            target=_pump_process_stdout,
            kwargs={
                "process": process,
                "use_pty": use_pty,
                "pty_master_fd": pty_master_fd,
                "emit": output.emit,
            },
            daemon=True,
        )
        reader.start()

        try:
            run_status = _poll_live_process(
                cmd=cmd,
                process=process,
                reader=reader,
                task_id=task_id,
                phase=phase,
                log_path=log_path,
                timeout=timeout,
                silence_timeout_seconds=silence_timeout_seconds,
                deps=deps,
                output=output,
                status=run_status,
            )
            handle.flush()
            output.finalize(
                status=run_status.state,
                exit_code=run_status.exit_code,
                detail=run_status.detail,
            )
            return int(run_status.exit_code or 0), log_path.read_text(encoding="utf-8", errors="replace").strip()
        finally:
            if run_status.state not in {"ok", "failed"}:
                output.finalize(
                    status=run_status.state,
                    exit_code=run_status.exit_code,
                    detail=run_status.detail,
                )
            if process.stdin:
                try:
                    process.stdin.close()
                except Exception:
                    pass
            if process.poll() is None:
                deps.stop_process_tree(process.pid)
            reader.join(timeout=2)
