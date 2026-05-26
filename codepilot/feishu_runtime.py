"""Feishu runtime paths — all-Python, no Node.js sidecar needed."""

from __future__ import annotations

import sys
from pathlib import Path


def runtime_root() -> Path:
    """Return the directory for feishu runtime files (logs, state)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def script_path(name: str) -> Path:
    return Path(__file__).resolve().parent / name


def worker_script() -> Path:
    return script_path("feishu_worker.py")


def notify_script() -> Path:
    return script_path("feishu_notify.py")
