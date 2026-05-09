from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codepilot.commands import chat as chat_cmd


class FakeMCPProcess:
    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return 0


class SlowMCPProcess(FakeMCPProcess):
    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired(cmd="codepilot mcp serve", timeout=timeout)
        return 0


def _isolate_chat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chat_cmd, "_project_record", lambda project: None)
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: None)
    monkeypatch.setattr(
        chat_cmd,
        "_resolve_agent_executable",
        lambda agent, cfg: f"{agent}-bin",
    )
    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", lambda *args, **kwargs: None)


def test_chat_starts_mcp_server_before_agent_and_cleans_up_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[tuple[str, list[str]]] = []
    mcp_process = FakeMCPProcess()

    def fake_popen(command, **kwargs):
        events.append(("mcp", list(command)))
        return mcp_process

    def fake_run(command, **kwargs):
        events.append(("agent", list(command)))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode")

    assert exit_code == 0
    assert [event[0] for event in events] == ["mcp", "agent"]
    assert events[0][1][1:] == [
        "-m",
        "codepilot",
        "mcp",
        "serve",
        "--transport",
        "stdio",
    ]
    assert mcp_process.terminate_calls == 1
    assert mcp_process.kill_calls == 0
    assert mcp_process.wait_calls == [pytest.approx(5)]


def test_chat_kills_mcp_server_when_terminate_wait_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    mcp_process = SlowMCPProcess()

    monkeypatch.setattr(
        chat_cmd.subprocess,
        "Popen",
        lambda command, **kwargs: mcp_process,
    )
    monkeypatch.setattr(
        chat_cmd.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode")

    assert exit_code == 0
    assert mcp_process.terminate_calls == 1
    assert mcp_process.kill_calls == 1
    assert mcp_process.wait_calls == [pytest.approx(5), pytest.approx(5)]


def test_chat_cleans_up_mcp_server_when_agent_launch_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[str] = []
    mcp_process = FakeMCPProcess()

    def fake_popen(command, **kwargs):
        events.append("mcp")
        return mcp_process

    def fake_run(command, **kwargs):
        events.append("agent")
        raise RuntimeError("agent failed")

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="agent failed"):
        chat_cmd._launch_mcp_agent_chat(agent="opencode")

    assert events == ["mcp", "agent"]
    assert mcp_process.terminate_calls == 1
    assert mcp_process.kill_calls == 0
    assert mcp_process.wait_calls == [pytest.approx(5)]
