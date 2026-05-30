from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.commands import chat as chat_cmd


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, name: str) -> None:
        self.name = name
        self.stdin = FakeStdin()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []
        self.returncode = 0

    def poll(self) -> int | None:
        return None if not self.wait_calls and not self.terminate_calls else self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return self.returncode


def _isolate_chat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, object]]:
    writes: list[dict[str, object]] = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chat_cmd, "_project_record", lambda project: None)
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: None)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)

    def fake_write(config_files: dict[str, str], *, cwd: Path) -> None:
        writes.append({"cwd": cwd, "files": dict(config_files)})

    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", fake_write)
    return writes


def test_agent_slash_command_stops_current_agent_and_starts_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    agent_processes: list[FakeProcess] = []
    events: list[tuple[str, list[str]]] = []
    resets: list[str] = []

    def fake_popen(command, **kwargs):
        command = list(command)
        process = FakeProcess(command[0])
        agent_processes.append(process)
        events.append(("agent", command))
        return process

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(chat_cmd, "_reset_terminal_after_tui", lambda: resets.append("reset"))

    exit_code = chat_cmd._run_mcp_agent_chat_session(
        agent="opencode",
        project=None,
        input_stream=iter(["hello\n", "/agent claude\n"]),
    )

    assert exit_code == 0
    assert [event[0] for event in events] == ["agent", "agent"]
    assert events[0][1][0] == "opencode-bin"
    assert events[1][1][0] == "claude-bin"
    assert agent_processes[0].stdin.writes == ["hello\n"]
    assert agent_processes[0].terminate_calls == 1
    assert resets == ["reset", "reset"]


def test_agent_slash_command_rejects_unknown_agent_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _isolate_chat(monkeypatch, tmp_path)
    agent_processes: list[FakeProcess] = []

    def fake_popen(command, **kwargs):
        command = list(command)
        process = FakeProcess(command[0])
        agent_processes.append(process)
        return process

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", fake_popen)

    exit_code = chat_cmd._run_mcp_agent_chat_session(
        agent="opencode",
        project=None,
        input_stream=iter(["/agent ghost\n"]),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert [process.name for process in agent_processes] == ["opencode-bin"]
    assert "不支持的 chat agent" in captured.err


def test_agent_switch_fresh_restart_reinjects_mcp_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    writes = _isolate_chat(monkeypatch, tmp_path)
    agent_commands: list[list[str]] = []

    def fake_popen(command, **kwargs):
        command = list(command)
        agent_commands.append(command)
        return FakeProcess(command[0])

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", fake_popen)

    exit_code = chat_cmd._run_mcp_agent_chat_session(
        agent="opencode",
        project=None,
        input_stream=iter(["/agent opencode\n", "/agent claude\n"]),
    )

    assert exit_code == 0
    assert [command[0] for command in agent_commands] == ["opencode-bin", "claude-bin"]
    claude_command = agent_commands[1]
    config_arg_index = claude_command.index("--mcp-config") + 1
    assert "codepilot" in claude_command[config_arg_index]
    assert len(writes) == 1
    runtime_dir = Path.home() / ".codepilot" / "opencode" / tmp_path.name
    assert str(runtime_dir / "opencode.json") in writes[0]["files"]
    assert str(runtime_dir / "tui.json") in writes[0]["files"]
    assert str(runtime_dir / "config" / "agents" / "codepilot.md") in writes[0]["files"]
    assert not (tmp_path / ".codepilot" / "opencode").exists()
