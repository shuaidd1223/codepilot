"""Runtime paths for Feishu's Node.js sidecar assets."""

from __future__ import annotations

import sys
from pathlib import Path


def runtime_root() -> Path:
    """Return the directory where Feishu package.json/node_modules should live."""
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        sidecar = executable_dir / "feishu"
        return sidecar if sidecar.exists() else executable_dir
    return Path(__file__).resolve().parents[1]


def script_path(name: str) -> Path:
    """Return a runnable Feishu .mjs script path."""
    sidecar = runtime_root() / name
    if sidecar.exists():
        return sidecar
    return Path(__file__).resolve().parent / name


def worker_script() -> Path:
    return script_path("feishu_worker.mjs")


def notify_script() -> Path:
    return script_path("feishu_notify.mjs")
