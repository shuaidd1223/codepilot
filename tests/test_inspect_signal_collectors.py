from __future__ import annotations

import subprocess
import sys

from codepilot.commands import inspect_signal_collectors as collectors


def test_collect_ruff_prefers_current_python_module(tmp_path, monkeypatch):
    commands: list[list[str]] = []

    monkeypatch.setattr(collectors, "_python_module_available", lambda module: module == "ruff")
    monkeypatch.setattr(collectors, "_which", lambda _cmd: "ruff")

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=b"demo.py:1:1: F401 unused import\n", stderr=b"")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)

    result = collectors.collect_ruff(tmp_path)

    assert commands[0][:3] == [sys.executable, "-m", "ruff"]
    assert "F401" in result
