"""Shared helpers for starting long-lived services outside the caller tree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from codepilot.core.runtime import is_process_alive
from codepilot.core.text_decode import decode_subprocess_text

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


class DetachedProcessHandle:
    """Small Popen-like handle for a process started by the launcher."""

    def __init__(self, pid: int):
        self.pid = int(pid)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if is_process_alive(self.pid):
            return None
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def append_log_header(log_file: Path, text: str) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_file, "ab") as log_fp:
            log_fp.write(text.encode("utf-8"))
            log_fp.flush()
    except Exception:
        pass


def spawn_detached_command_via_launcher(
    cmd: list[str],
    *,
    log_file: Path,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Start a command outside the caller's live process tree and return its PID."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cwd_text = str(cwd) if cwd is not None else None
    env_payload = {str(key): str(value) for key, value in env.items()} if env is not None else None

    if os.name != "nt":
        log_fp = open(log_file, "ab")
        popen_env = env_payload
        if getattr(sys, "frozen", False):
            popen_env = os.environ.copy()
            if env_payload:
                popen_env.update(env_payload)
            popen_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            close_fds=True,
            cwd=cwd_text,
            env=popen_env,
            start_new_session=True,
        )
        return int(proc.pid)

    if getattr(sys, "frozen", False):
        log_fp = open(log_file, "ab")
        popen_env = os.environ.copy()
        if env_payload:
            popen_env.update(env_payload)
        popen_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            close_fds=True,
            cwd=cwd_text,
            env=popen_env,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
        return int(proc.pid)

    launcher = r"""
import json
import subprocess
import sys

payload = json.loads(sys.argv[1])
log_fp = open(payload["log"], "ab")
proc = subprocess.Popen(
    payload["cmd"],
    stdin=subprocess.DEVNULL,
    stdout=log_fp,
    stderr=log_fp,
    close_fds=True,
    cwd=payload.get("cwd") or None,
    env=payload.get("env") or None,
    creationflags=0x00000008 | 0x00000200,
)
print(proc.pid)
"""
    payload = {"cmd": cmd, "log": str(log_file), "cwd": cwd_text, "env": env_payload}
    result = subprocess.run(
        [sys.executable, "-c", launcher, json.dumps(payload, ensure_ascii=False)],
        capture_output=True,
        text=False,
        timeout=15,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        detail = (decode_subprocess_text(result.stderr) or decode_subprocess_text(result.stdout)).strip()
        raise RuntimeError(detail or f"启动器退出，exit={result.returncode}")
    raw = decode_subprocess_text(result.stdout).strip().splitlines()
    try:
        return int(raw[-1])
    except Exception as exc:
        raise RuntimeError(f"启动器未返回 PID：{decode_subprocess_text(result.stdout).strip()}") from exc
