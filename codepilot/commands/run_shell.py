"""Shell detection + subprocess command execution helpers.

Split out from run.py for maintainability. Re-exported by run.py so existing
`from codepilot.commands.run import X` keeps working.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from codepilot.output import echo
from codepilot.runtime import (
    HEARTBEAT_INTERVAL_SECONDS,
    get_stop_request,
    stop_process_tree,
    tail_text,
    update_task_runtime,
)


@dataclass
class ShellInfo:
    """Resolved shell configuration for dispatch scripts."""

    executable: str
    args: list[str]
    is_powershell: bool = False
    is_bash: bool = False
    version_hint: str = ""


class TaskCancelled(RuntimeError):
    """Raised when a running task is explicitly stopped."""


def detect_best_shell(preferred: Optional[str] = None) -> ShellInfo:
    """Pick the best available shell on the current platform."""
    import platform
    import shutil

    system = platform.system().lower()
    is_windows = system == "windows"
    requested = (preferred or "").lower().strip()

    if requested in {"pwsh", "powershell7"} and shutil.which("pwsh"):
        return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
    if requested == "powershell":
        if shutil.which("powershell.exe"):
            return ShellInfo("powershell.exe", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 5")
        if shutil.which("pwsh"):
            return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
    if requested in {"bash", "zsh", "sh"}:
        shell = shutil.which(requested)
        if shell:
            return ShellInfo(shell, ["-c"], is_bash=True, version_hint=requested)

    if is_windows:
        if shutil.which("pwsh"):
            return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
        if shutil.which("powershell.exe"):
            return ShellInfo("powershell.exe", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 5")
        return ShellInfo("cmd.exe", ["/C"], version_hint="cmd")

    for shell_name in ["zsh", "bash", "sh"]:
        shell = shutil.which(shell_name)
        if shell:
            return ShellInfo(shell, ["-c"], is_bash=True, version_hint=shell_name)
    return ShellInfo("sh", ["-c"], is_bash=True, version_hint="sh")


def build_script_command(shell: ShellInfo, script_path: Path, script_args: list[str]) -> tuple[list[str], str]:
    """Build a portable script invocation command."""
    if shell.is_powershell:
        cmd = [shell.executable] + shell.args + ["-File", str(script_path)] + script_args
        return cmd, f"{shell.version_hint} -File {script_path.name}"
    if shell.is_bash:
        quoted_args = " ".join(f'"{arg}"' for arg in script_args)
        script = f'chmod +x "{script_path}" 2>/dev/null; "{script_path}" {quoted_args}'
        cmd = [shell.executable] + shell.args + [script]
        return cmd, f"{shell.version_hint} {script_path.name}"
    cmd = [shell.executable] + shell.args + [f'"{script_path}" {" ".join(script_args)}']
    return cmd, f"cmd {script_path.name}"


def _run_command(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = 3600,
    input_text: Optional[str] = None,
) -> tuple[int, str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        input=input_text,
    )
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    return result.returncode, output



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
        meaningful = [l.strip() for l in lines if l.strip() and not l.strip().startswith(("import ", "from ", "def ", "class ", "    "))]
        summary = meaningful[-5:]
    return summary[:15]



def _run_command_live(
    cmd: list[str],
    *,
    task_id: int,
    phase: str,
    log_path: Path,
    cwd: Optional[Path] = None,
    timeout: int = 3600,
    input_text: Optional[str] = None,
) -> tuple[int, str]:
    """Run a long-lived command while streaming output, updating heartbeat, and honoring stop requests."""
    # Late-lookup so tests that monkeypatch `codepilot.commands.run.<helper>` take effect
    # even though this function lives in run_shell.py.
    from codepilot.commands import run as _rc
    get_stop_request = _rc.get_stop_request
    stop_process_tree = _rc.stop_process_tree
    update_task_runtime = _rc.update_task_runtime
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 可选 PTY：某些 CLI（codex/claude）在非 TTY 下会切块缓冲，用伪终端可以让它按行刷。
    # 仅在 Unix 且显式开启环境变量 CODEPILOT_USE_PTY=1 时启用，Windows 默认保持 Popen 管道。
    use_pty = (
        os.name != "nt"
        and os.environ.get("CODEPILOT_USE_PTY", "").strip() in {"1", "true", "yes"}
    )
    with log_path.open("w", encoding="utf-8", errors="replace") as handle:
        popen_kwargs = {
            "cwd": str(cwd) if cwd else None,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.PIPE if input_text is not None else None,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        pty_master_fd: Optional[int] = None
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
                from codepilot.runtime import no_window_kwargs
                popen_kwargs.update(no_window_kwargs())

        process = subprocess.Popen(cmd, **popen_kwargs)
        if use_pty:
            # 关掉父进程这端的 slave，让子进程退出时 read 能收到 EOF
            try:
                os.close(pty_slave_fd)
            except Exception:
                pass
        if input_text is not None and process.stdin:
            process.stdin.write(input_text)
            process.stdin.close()

        update_task_runtime(task_id, phase=phase, pid=process.pid, log_path=log_path, last_output="")

        recent_lines: list[str] = []
        recent_lock = threading.Lock()

        def _emit(raw: str) -> None:
            if not raw:
                return
            try:
                handle.write(raw)
                handle.flush()
            except Exception:
                pass
            stripped = raw.rstrip()
            if stripped:
                # Only show concise progress lines, not full code output
                _show = _should_show_line(stripped)
                if _show:
                    try:
                        from rich.markup import escape as _rich_escape
                        STATUS_CONSOLE.print(f"    [dim]{_rich_escape(stripped[:160])}[/dim]")
                    except Exception:
                        pass
            with recent_lock:
                recent_lines.append(raw)
                if len(recent_lines) > 200:
                    del recent_lines[:-200]

        def _pump_stdout() -> None:
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
                        _emit(line.decode("utf-8", errors="replace") + "\n")
                if buf:
                    _emit(buf.decode("utf-8", errors="replace"))
                try:
                    os.close(pty_master_fd)
                except Exception:
                    pass
                return
            assert process.stdout is not None
            for raw in process.stdout:
                _emit(raw)

        reader = threading.Thread(target=_pump_stdout, daemon=True)
        reader.start()

        started = time.monotonic()
        last_heartbeat = 0.0

        try:
            while True:
                requested, reason = get_stop_request(task_id)
                if requested:
                    stop_process_tree(process.pid)
                    reader.join(timeout=2)
                    update_task_runtime(
                        task_id,
                        phase=phase,
                        pid=process.pid,
                        log_path=log_path,
                        last_output=tail_text(log_path),
                    )
                    raise TaskCancelled(reason or f"任务 #{task_id} 已停止")

                if time.monotonic() - started > timeout:
                    stop_process_tree(process.pid)
                    reader.join(timeout=2)
                    raise subprocess.TimeoutExpired(cmd, timeout)

                exit_code = process.poll()
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    with recent_lock:
                        preview = "".join(recent_lines[-20:]).strip()
                    update_task_runtime(
                        task_id,
                        phase=phase,
                        pid=process.pid if exit_code is None else None,
                        log_path=log_path,
                        last_output=preview,
                    )
                    last_heartbeat = now

                if exit_code is not None:
                    reader.join(timeout=5)
                    handle.flush()
                    update_task_runtime(
                        task_id,
                        phase=phase,
                        pid=None,
                        log_path=log_path,
                        last_output=tail_text(log_path),
                    )
                    return exit_code, log_path.read_text(encoding="utf-8", errors="replace").strip()

                time.sleep(0.1)
        finally:
            if process.stdin:
                try:
                    process.stdin.close()
                except Exception:
                    pass
            if process.poll() is None:
                stop_process_tree(process.pid)
            reader.join(timeout=2)
