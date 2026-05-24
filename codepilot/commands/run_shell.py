"""Shell detection + subprocess command execution helpers.

Split out from run.py for maintainability. Re-exported by run.py so existing
`from codepilot.commands.run import X` keeps working.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from codepilot.core.text_decode import decode_subprocess_text


@dataclass
class ShellInfo:
    """Resolved shell configuration for dispatch scripts."""

    executable: str
    args: list[str]
    is_powershell: bool = False
    is_bash: bool = False
    version_hint: str = ""


class PreflightSkipError(RuntimeError):
    """Raised when preflight says skip-without-consuming-retry.

    Generic RuntimeError gets treated as real execution failure and bumps
    ``retry_count`` in ``_handle_failure``. Preflight-level skips (dirty
    working tree, no git repo, etc.) are environmental and explicitly
    advertise ``不消耗重试次数``; catching this distinct exception in the
    run loop lets us requeue the task to backlog without retry consumption.
    """


def detect_best_shell(preferred: Optional[str] = None) -> ShellInfo:
    """Pick the best available shell on the current platform."""
    import platform

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


# Env vars forced on every ``git`` invocation so the subprocess can never
# hang waiting for a human. If credentials / a GPG passphrase are needed
# and not available via the environment, the command fails fast with a
# visible error instead of blocking forever. ``SSH_ASKPASS`` needs a
# companion ``DISPLAY`` / ``SSH_ASKPASS_REQUIRE=never`` to take effect on
# OpenSSH ≥ 8.4, so we set both.
_GIT_NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "SSH_ASKPASS": "echo",
    "SSH_ASKPASS_REQUIRE": "never",
    "GCM_INTERACTIVE": "Never",        # Git Credential Manager
    "GIT_OPTIONAL_LOCKS": "0",         # skip optional locks that can stall
}


def _run_command(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = 3600,
    input_text: Optional[str] = None,
) -> tuple[int, str]:
    env = None
    if cmd and isinstance(cmd[0], str) and os.path.basename(cmd[0]).lower().startswith("git"):
        # Merge — inherit the user's env, overlay the non-interactive flags.
        env = {**os.environ, **_GIT_NONINTERACTIVE_ENV}
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=False,
        timeout=timeout,
        input=(input_text.encode("utf-8") if input_text is not None else None),
        env=env,
    )
    stdout_text = decode_subprocess_text(result.stdout)
    stderr_text = decode_subprocess_text(result.stderr)
    output = (stdout_text + "\n" + stderr_text).strip()
    return result.returncode, output

