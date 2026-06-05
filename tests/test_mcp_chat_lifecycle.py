from __future__ import annotations

from contextlib import contextmanager
import json
import subprocess
import sys
import types
from pathlib import Path

import click
import click.testing
import pytest

from codepilot.commands import chat as chat_cmd
from codepilot.core.config import AgentsConfig


@pytest.fixture(autouse=True)
def _isolate_codepilot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))


class FakeAgentProcess:
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


def _isolate_chat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chat_cmd, "_project_record", lambda project: None)
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: None)
    monkeypatch.setattr(
        chat_cmd,
        "_resolve_agent_executable",
        lambda agent, cfg: f"{agent}-bin",
    )
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", lambda *args, **kwargs: None)


def test_chat_launches_agent_without_unattached_mcp_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[list[str]] = []

    def fake_popen(command, **kwargs):
        raise AssertionError(f"unexpected unmanaged MCP process: {command}")

    def fake_run(command, **kwargs):
        events.append(list(command))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode")

    assert exit_code == 0
    assert [event[0] for event in events] == ["opencode-bin"]


def test_windows_codex_interactive_launch_uses_argument_vector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[dict[str, object]] = []

    monkeypatch.setattr(chat_cmd.os, "name", "nt")

    def fake_run(command, **kwargs):
        events.append({"command": command, "kwargs": kwargs})
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)

    assert chat_cmd._launch_mcp_agent_chat(agent="codex") == 0

    assert len(events) == 1
    assert str(events[0]["command"][0]).lower().endswith(("codex-bin", "codex.exe"))
    assert "-c" in events[0]["command"]
    assert "shell" not in events[0]["kwargs"]


def test_windows_codex_snapshots_and_restores_console_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    mode_writes: list[int] = []

    class FakeKernel32:
        def GetStdHandle(self, value):
            return 123

        def GetConsoleMode(self, handle, mode_ptr):
            mode_ptr._obj.value = 0x0200 | 0x0004
            return 1

        def SetConsoleMode(self, handle, mode):
            mode_writes.append(int(mode))
            return 1

    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(kernel32=FakeKernel32()),
        c_uint32=lambda: types.SimpleNamespace(value=0),
        byref=lambda value: types.SimpleNamespace(_obj=value),
    )

    monkeypatch.setattr(chat_cmd.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setattr(
        chat_cmd.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    assert chat_cmd._launch_mcp_agent_chat(agent="codex") == 0

    # No preemptive flag changes — only restorations after codex exits.
    # Input handle restore + output handle restore.
    assert mode_writes == [0x0204, 0x0204]


def test_chat_uses_detected_current_project_for_mcp_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: None)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", lambda *args, **kwargs: None)
    writes: list[dict[str, str]] = []

    def fake_popen(command, **kwargs):
        raise AssertionError(f"unexpected unmanaged MCP process: {command}")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0)

    def fake_write(config_files: dict[str, str], **kwargs):
        writes.append(dict(config_files))

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)
    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", fake_write)

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode")

    assert exit_code == 0
    rendered_config = "\n".join(value for item in writes for value in item.values())
    assert '"--project"' in rendered_config
    assert '"demo"' in rendered_config


def test_chat_uses_registered_project_name_for_explicit_alias_mcp_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "codepilot-dev", "path": str(tmp_path)},
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: None)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    writes: list[dict[str, str]] = []

    def fake_write(config_files: dict[str, str], **kwargs):
        writes.append(dict(config_files))

    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", fake_write)
    monkeypatch.setattr(
        chat_cmd.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode", project="flower")

    assert exit_code == 0
    rendered_config = "\n".join(value for item in writes for value in item.values())
    assert '"--project"' in rendered_config
    assert '"codepilot-dev"' in rendered_config
    assert '"flower"' not in rendered_config


def test_chat_uses_tool_level_opencode_profile_not_project_opencode_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    project_cfg = AgentsConfig.from_dict(
        {
            "opencode": {
                "profile": {"brand_name": "ProjectBrand", "default_agent": "project-agent"},
                "agent": {"name": "project-agent", "prompt": "Project prompt."},
            }
        }
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: project_cfg)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    writes: list[dict[str, str]] = []

    def fake_write(config_files: dict[str, str], **kwargs):
        writes.append(dict(config_files))

    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", fake_write)

    launch = chat_cmd._prepare_mcp_agent_chat(agent="opencode", project=None, prompt="")

    assert launch.command == ["opencode-bin", "--agent", "codepilot"]
    rendered_config = "\n".join(value for item in writes for value in item.values())
    assert "ProjectBrand" not in rendered_config
    assert "project-agent" not in rendered_config
    opencode_json_path = str(tmp_path / ".codepilot" / "opencode.json")
    payload = json.loads(writes[0][opencode_json_path])
    assert payload["default_agent"] == "codepilot"
    assert not (tmp_path / ".codepilot" / "opencode").exists()


def test_chat_opencode_launch_writes_project_permission_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    project_cfg = AgentsConfig.from_dict(
        {
            "opencode": {
                "permission": {"mode": "full_access"},
            }
        }
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: project_cfg)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    writes: list[dict[str, str]] = []

    def fake_write(config_files: dict[str, str], **kwargs):
        writes.append(dict(config_files))

    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", fake_write)

    launch = chat_cmd._prepare_mcp_agent_chat(agent="opencode", project=None, prompt="")

    assert launch.command == ["opencode-bin", "--agent", "codepilot"]
    opencode_json_path = str(tmp_path / ".codepilot" / "opencode.json")
    payload = json.loads(writes[0][opencode_json_path])
    assert payload["permission"] == "allow"


def test_chat_opencode_launch_maps_deepseek_to_custom_provider_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    project_cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "deepseek": {
                    "enabled": True,
                    "api_key": "sk-ds-test",
                    "base_url": "https://api.deepseek.com",
                }
            }
        }
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: project_cfg)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", lambda *args, **kwargs: None)

    launch = chat_cmd._prepare_mcp_agent_chat(agent="opencode", project=None, prompt="")

    assert launch.env["DEEPSEEK_API_KEY"] == "sk-ds-test"
    assert "OPENAI_API_KEY" not in launch.env
    assert "OPENAI_BASE_URL" not in launch.env
    assert "DEEPSEEK_BASE_URL" not in launch.env


def test_chat_opencode_launch_writes_deepseek_custom_provider_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    project_cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "deepseek": {
                    "enabled": True,
                    "api_key": "sk-ds-test",
                    "base_url": "https://api.deepseek.com",
                    "simple_model": "deepseek-v4-flash",
                    "complex_model": "deepseek-v4-pro",
                    "thinking": "auto",
                    "reasoning_effort": "auto",
                }
            }
        }
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: project_cfg)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    writes: list[dict[str, str]] = []

    def fake_write(config_files: dict[str, str], **kwargs):
        writes.append(dict(config_files))

    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", fake_write)

    chat_cmd._prepare_mcp_agent_chat(agent="opencode", project=None, prompt="")

    opencode_json_path = str(tmp_path / ".codepilot" / "opencode.json")
    payload = json.loads(writes[0][opencode_json_path])
    assert payload["model"] == "deepseek/deepseek-v4-pro"
    assert payload["small_model"] == "deepseek/deepseek-v4-flash"
    assert payload["agent"]["codepilot"]["model"] == "deepseek/deepseek-v4-pro"
    assert "enabled_providers" not in payload
    assert payload["provider"]["deepseek"]["npm"] == "@ai-sdk/openai-compatible"
    assert payload["provider"]["deepseek"]["options"]["baseURL"] == "https://api.deepseek.com"
    assert payload["provider"]["deepseek"]["options"]["apiKey"] == "{env:DEEPSEEK_API_KEY}"
    assert "openai" not in payload["provider"]


def test_chat_opencode_launch_writes_configured_openai_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    project_cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "openai": {
                    "enabled": True,
                    "api_key": "sk-test",
                    "base_url": "https://sub.hdd.sb/v1",
                    "model": "gpt-5.4",
                }
            }
        }
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: project_cfg)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    writes: list[dict[str, str]] = []

    def fake_write(config_files: dict[str, str], **kwargs):
        writes.append(dict(config_files))

    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", fake_write)

    chat_cmd._prepare_mcp_agent_chat(agent="opencode", project=None, prompt="")

    opencode_json_path = str(tmp_path / ".codepilot" / "opencode.json")
    payload = json.loads(writes[0][opencode_json_path])
    assert payload["model"] == "openai/gpt-5.4"
    assert payload["agent"]["codepilot"]["model"] == "openai/gpt-5.4"


def test_chat_opencode_launch_uses_project_model_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    project_cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "openai": {
                    "enabled": True,
                    "api_key": "sk-test",
                    "base_url": "https://sub.hdd.sb/v1",
                    "model": "gpt-5.4",
                }
            }
        }
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: project_cfg)
    seen_model_lookup: dict[str, object] = {}

    def fake_project_model(scope, project_path, *, db_path=None):
        seen_model_lookup["scope"] = scope
        seen_model_lookup["project_path"] = Path(project_path)
        seen_model_lookup["db_path"] = Path(db_path)
        return {"provider_id": "deepseek", "model_id": "deepseek-v4-pro"}

    monkeypatch.setattr(chat_cmd, "resolve_project_model_selection", fake_project_model)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    writes: list[dict[str, str]] = []

    def fake_write(config_files: dict[str, str], **kwargs):
        writes.append(dict(config_files))

    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", fake_write)

    chat_cmd._prepare_mcp_agent_chat(agent="opencode", project=None, prompt="")

    opencode_json_path = str(tmp_path / ".codepilot" / "opencode.json")
    payload = json.loads(writes[0][opencode_json_path])
    assert payload["model"] == "deepseek/deepseek-v4-pro"
    assert payload["agent"]["codepilot"]["model"] == "deepseek/deepseek-v4-pro"
    assert seen_model_lookup["scope"] == "demo"
    assert seen_model_lookup["project_path"] == tmp_path.resolve()
    assert seen_model_lookup["db_path"] == (
        Path.home() / ".codepilot" / "opencode" / "demo" / "xdg-data" / "opencode" / "opencode.db"
    )


def test_chat_opencode_session_resume_uses_codepilot_command_and_isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: None)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", lambda *args, **kwargs: None)

    launch = chat_cmd._prepare_mcp_agent_chat(
        agent="opencode",
        project=None,
        prompt="",
        session="ses_123",
    )

    assert launch.command == ["opencode-bin", "--agent", "codepilot", "-s", "ses_123"]
    assert launch.env["OPENCODE_CONFIG"] == str(tmp_path / ".codepilot" / "opencode.json")
    assert launch.env["XDG_DATA_HOME"] == str(Path.home() / ".codepilot" / "opencode" / "demo" / "xdg-data")
    assert launch.opencode_db_path == (
        Path.home() / ".codepilot" / "opencode" / "demo" / "xdg-data" / "opencode" / "opencode.db"
    )


def test_chat_prints_codepilot_resume_command_after_opencode_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _isolate_chat(monkeypatch, tmp_path)
    monkeypatch.setattr(
        chat_cmd.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode", session="ses_123")

    assert exit_code == 0
    output = capsys.readouterr().err
    assert "CodePilot" in output
    assert "ses_123" in output
    assert "codepilot chat --session ses_123" in output


def test_chat_syncs_project_model_after_opencode_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    synced: list[dict[str, object]] = []

    def fake_sync(scope, project_path, *, db_path=None):
        synced.append(
            {
                "scope": scope,
                "project_path": Path(project_path),
                "db_path": Path(db_path),
            }
        )
        return {"provider_id": "deepseek", "model_id": "deepseek-v4-pro"}

    monkeypatch.setattr(chat_cmd, "sync_latest_project_model_selection", fake_sync)
    monkeypatch.setattr(
        chat_cmd.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    assert chat_cmd._launch_mcp_agent_chat(agent="opencode") == 0

    assert synced == [
        {
            "scope": tmp_path.name,
            "project_path": tmp_path.resolve(),
            "db_path": Path.home()
            / ".codepilot"
            / "opencode"
            / tmp_path.name
            / "xdg-data"
            / "opencode"
            / "opencode.db",
        }
    ]


def test_chat_resets_terminal_without_clearing_exit_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    writes: list[str] = []

    class TtyStdout:
        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            writes.append(value)
            return len(value)

        def flush(self) -> None:
            writes.append("<flush>")

    monkeypatch.setattr(chat_cmd.sys, "stdout", TtyStdout())
    monkeypatch.setattr(
        chat_cmd.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode", session="ses_123")

    assert exit_code == 0
    rendered = "".join(writes)
    assert "\x1b[?1006l" in rendered
    assert "\x1b[?1003l" in rendered
    assert "\x1b[?25h" in rendered
    assert "\x1b[?1049l" in rendered       # exit alternate screen
    assert "\x1b[?2004l" in rendered       # disable bracketed paste
    assert "\x1b[2J\x1b[H" not in rendered


def test_chat_opencode_launch_injects_all_configured_provider_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "_project_record",
        lambda project: {"name": "demo", "path": str(tmp_path)},
    )
    project_cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "deepseek": {
                    "enabled": True,
                    "api_key": "sk-ds-test",
                    "base_url": "https://api.deepseek.com",
                    "complex_model": "deepseek-v4-pro",
                },
                "qwen": {
                    "enabled": True,
                    "api_key": "sk-qwen-test",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "model": "qwen-plus",
                },
            }
        }
    )
    monkeypatch.setattr(chat_cmd, "load_project_config", lambda record_or_path: project_cfg)
    monkeypatch.setattr(chat_cmd, "_resolve_agent_executable", lambda agent, cfg: f"{agent}-bin")
    monkeypatch.setattr(chat_cmd, "_ensure_mcp_sdk_available", lambda: None)
    monkeypatch.setattr(chat_cmd, "_write_launch_config_files", lambda *args, **kwargs: None)

    launch = chat_cmd._prepare_mcp_agent_chat(agent="opencode", project=None, prompt="")

    assert launch.env["DEEPSEEK_API_KEY"] == "sk-ds-test"
    assert launch.env["DASHSCOPE_API_KEY"] == "sk-qwen-test"
    assert "OPENAI_API_KEY" not in launch.env


def test_chat_does_not_start_unmanaged_mcp_server_for_agent_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)

    monkeypatch.setattr(
        chat_cmd.subprocess,
        "Popen",
        lambda command, **kwargs: (_ for _ in ()).throw(AssertionError(f"unexpected Popen: {command}")),
    )
    monkeypatch.setattr(
        chat_cmd.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode")

    assert exit_code == 0


def test_chat_agent_launch_raises_without_unmanaged_mcp_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[str] = []

    def fake_popen(command, **kwargs):
        raise AssertionError(f"unexpected unmanaged MCP process: {command}")

    def fake_run(command, **kwargs):
        events.append("agent")
        raise RuntimeError("agent failed")

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)
    monkeypatch.setattr(chat_cmd, "_reset_terminal_after_tui", lambda: events.append("reset"))

    with pytest.raises(RuntimeError, match="agent failed"):
        chat_cmd._launch_mcp_agent_chat(agent="opencode")

    assert events == ["agent", "reset"]


def test_chat_launch_resets_terminal_after_tui_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[str] = []

    def fake_run(command, **kwargs):
        events.append("run")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)
    monkeypatch.setattr(chat_cmd, "_reset_terminal_after_tui", lambda: events.append("reset"))

    assert chat_cmd._launch_mcp_agent_chat(agent="opencode") == 0
    assert events == ["run", "reset"]


def test_chat_session_cleans_up_agent_and_mcp_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    agent_process = FakeAgentProcess()

    monkeypatch.setattr(chat_cmd.subprocess, "Popen", lambda command, **kwargs: agent_process)

    def interrupting_input():
        raise KeyboardInterrupt
        yield ""  # pragma: no cover

    with pytest.raises(KeyboardInterrupt):
        chat_cmd._run_mcp_agent_chat_session(
            agent="opencode",
            input_stream=interrupting_input(),
        )

    assert agent_process.terminate_calls == 1


def test_chat_command_preserves_tty_for_default_interactive_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    class TtyStdin:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(chat_cmd.sys, "stdin", TtyStdin())

    def fake_run_session(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(chat_cmd, "_run_mcp_agent_chat_session", fake_run_session)

    with click.Context(chat_cmd.chat):
        chat_cmd.chat.callback(project=None, agent="opencode")

    assert captured["agent"] == "opencode"
    assert captured["input_stream"] is None


def test_chat_command_preserves_interactive_launch_when_stdin_is_not_tty_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    class NonTtyStdin:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(chat_cmd.sys, "stdin", NonTtyStdin())

    def fake_run_session(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(chat_cmd, "_run_mcp_agent_chat_session", fake_run_session)

    with click.Context(chat_cmd.chat):
        chat_cmd.chat.callback(project=None, agent="codex")

    assert captured["agent"] == "codex"
    assert captured["input_stream"] is None


def test_chat_command_allows_opt_in_stdin_forwarding_for_scripted_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    class NonTtyStdin:
        def isatty(self) -> bool:
            return False

    stdin = NonTtyStdin()
    monkeypatch.setenv("CODEPILOT_CHAT_STDIN_FORWARDING", "1")
    monkeypatch.setattr(chat_cmd.sys, "stdin", stdin)

    def fake_run_session(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(chat_cmd, "_run_mcp_agent_chat_session", fake_run_session)

    with click.Context(chat_cmd.chat):
        chat_cmd.chat.callback(project=None, agent="opencode")

    assert captured["input_stream"] is stdin


def test_chat_launch_feedback_stops_before_agent_takes_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[tuple[str, str]] = []

    @contextmanager
    def fake_status(message: str):
        events.append(("enter", message))
        yield
        events.append(("exit", message))

    def fake_notice(message: str) -> None:
        events.append(("notice", message))

    def fake_clear_notice() -> None:
        events.append(("clear", "notice"))

    def fake_run(command, **kwargs):
        assert ("exit", "正在准备 CodePilot MCP 工具...") in events
        assert ("clear", "notice") in events
        events.append(("run", command[0]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(chat_cmd, "_chat_startup_status", fake_status)
    monkeypatch.setattr(chat_cmd, "_chat_startup_notice", fake_notice)
    monkeypatch.setattr(chat_cmd, "_clear_chat_startup_notice", fake_clear_notice)
    monkeypatch.setattr(chat_cmd, "_set_terminal_title", lambda title: events.append(("title", title)))
    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)

    exit_code = chat_cmd._launch_mcp_agent_chat(agent="opencode")

    assert exit_code == 0
    assert events == [
        ("enter", "正在准备 CodePilot MCP 工具..."),
        ("exit", "正在准备 CodePilot MCP 工具..."),
        ("title", "CodePilot"),
        ("notice", "正在启动 CodePilot TUI，初始化 MCP 可能需要几秒..."),
        ("clear", "notice"),
        ("run", "opencode-bin"),
    ]


def test_chat_clears_startup_frame_before_opencode_takes_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[str] = []

    monkeypatch.setattr(chat_cmd, "_chat_startup_notice", lambda message: events.append("notice"))
    monkeypatch.setattr(chat_cmd, "_clear_chat_startup_notice", lambda: events.append("clear"))

    def fake_run(command, **kwargs):
        events.append("run")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(chat_cmd.subprocess, "run", fake_run)

    assert chat_cmd._launch_mcp_agent_chat(agent="opencode") == 0
    assert events[:3] == ["notice", "clear", "run"]


def test_chat_startup_notice_is_transient_on_tty(monkeypatch: pytest.MonkeyPatch):
    writes: list[str] = []

    class TtyStderr:
        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            writes.append(value)
            return len(value)

        def flush(self) -> None:
            writes.append("<flush>")

    monkeypatch.setattr(chat_cmd.sys, "stderr", TtyStderr())

    chat_cmd._chat_startup_notice("正在启动 CodePilot TUI，初始化 MCP 可能需要几秒...")
    chat_cmd._clear_chat_startup_notice()

    rendered = "".join(writes)
    assert "CODEPILOT" in rendered
    assert "_____ ____  _____  ______ _____ _____ _      ____ _______" in rendered
    assert "| |___| |__| | |__| | |____| |    _| |_| |___" in rendered
    assert "██████╗" not in rendered
    assert "正在启动 CodePilot TUI，初始化 MCP 可能需要几秒..." in rendered
    assert "█" in rendered
    assert "\x1b[?1049h" in rendered
    assert "\x1b[?1049l" in rendered
    assert "\x1b[?25l" in rendered
    assert "\x1b[?25h" in rendered
    assert "\x1b[2J\x1b[H" in rendered


def test_chat_does_not_clear_whole_terminal_before_resume_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    events: list[str] = []

    monkeypatch.setattr(chat_cmd, "_chat_startup_notice", lambda message: events.append("notice"))
    monkeypatch.setattr(chat_cmd, "_clear_chat_startup_notice", lambda: events.append("clear_startup"))
    monkeypatch.setattr(chat_cmd, "_clear_terminal_screen_for_codepilot", lambda: events.append("clear_terminal"))
    monkeypatch.setattr(chat_cmd, "_print_codepilot_resume_hint", lambda launch, project=None: events.append("resume"))
    monkeypatch.setattr(
        chat_cmd.subprocess,
        "run",
        lambda command, **kwargs: events.append("run") or subprocess.CompletedProcess(command, 0),
    )

    assert chat_cmd._launch_mcp_agent_chat(agent="opencode") == 0
    assert events == ["notice", "clear_startup", "run", "resume"]


def test_clear_terminal_screen_for_codepilot_resets_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch):
    stdout_writes: list[str] = []
    stderr_writes: list[str] = []

    class TtyStream:
        def __init__(self, writes: list[str]) -> None:
            self.writes = writes

        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            self.writes.append(value)
            return len(value)

        def flush(self) -> None:
            self.writes.append("<flush>")

    monkeypatch.setattr(chat_cmd.sys, "stdout", TtyStream(stdout_writes))
    monkeypatch.setattr(chat_cmd.sys, "stderr", TtyStream(stderr_writes))

    chat_cmd._clear_terminal_screen_for_codepilot()

    rendered = "".join(stdout_writes + stderr_writes)
    assert "\x1b[2J\x1b[H" in rendered
    assert "\x1b[?1006l" in rendered
    assert "\x1b[?25h" in rendered


def test_chat_resume_hint_renders_codepilot_panel(capsys: pytest.CaptureFixture[str]):
    launch = chat_cmd._PreparedChatLaunch(
        agent="opencode",
        command=["opencode"],
        cwd=Path.cwd(),
        env={},
        opencode_db_path=Path("missing.db"),
        requested_session="ses_panel",
    )

    chat_cmd._print_codepilot_resume_hint(launch, project="demo")

    output = capsys.readouterr().err
    assert "╭" in output
    assert "╰" in output
    assert "CodePilot 会话已暂停" in output
    assert "ses_panel" in output
    assert "codepilot chat --project demo --session ses_panel" in output
    assert "隔离配置" not in output
    assert "MCP" not in output
    assert "OpenCode 原生命令" not in output


def test_chat_resume_hint_renders_codepilot_panel_for_claude(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    launch = chat_cmd._PreparedChatLaunch(
        agent="claude",
        command=["claude"],
        cwd=tmp_path,
        env={},
        requested_session="ses_claude",
    )

    chat_cmd._print_codepilot_resume_hint(launch, project="demo")

    output = capsys.readouterr().err
    assert "CodePilot 会话已暂停" in output
    assert "ses_claude" in output
    assert "codepilot chat --project demo --session ses_claude" in output
    assert "--agent" not in output


def test_chat_resume_hint_for_claude_uses_latest_session_from_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_lookup_calls: list[tuple[Path, Path]] = []

    def fake_latest(project_path, *, projects_root=None):
        fake_lookup_calls.append((Path(project_path), Path(projects_root) if projects_root else Path()))
        return "ses_from_disk"

    monkeypatch.setattr(chat_cmd, "latest_claude_session_id", fake_latest)

    launch = chat_cmd._PreparedChatLaunch(
        agent="claude",
        command=["claude"],
        cwd=tmp_path,
        env={},
    )

    chat_cmd._print_codepilot_resume_hint(launch, project=None)

    assert fake_lookup_calls and fake_lookup_calls[0][0] == tmp_path
    output = capsys.readouterr().err
    assert "ses_from_disk" in output
    assert "codepilot chat --session ses_from_disk" in output
    assert "--agent" not in output


def test_detect_session_agent_returns_claude_when_jsonl_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(chat_cmd, "_project_record", lambda project: {"name": "demo", "path": str(tmp_path)})
    monkeypatch.setattr(chat_cmd, "claude_session_exists", lambda cwd, sid: Path(cwd) == tmp_path and sid == "ses-x")
    monkeypatch.setattr(chat_cmd, "_opencode_session_exists_for_scope", lambda scope, cwd, sid: False)
    monkeypatch.setattr(chat_cmd, "codex_session_exists", lambda cwd, sid: False)

    assert chat_cmd._detect_session_agent(session="ses-x", project=None) == "claude"


def test_detect_session_agent_returns_opencode_when_db_has_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(chat_cmd, "_project_record", lambda project: {"name": "demo", "path": str(tmp_path)})
    monkeypatch.setattr(chat_cmd, "claude_session_exists", lambda cwd, sid: False)
    monkeypatch.setattr(chat_cmd, "codex_session_exists", lambda cwd, sid: False)
    monkeypatch.setattr(
        chat_cmd,
        "_opencode_session_exists_for_scope",
        lambda scope, cwd, sid: scope == "demo" and Path(cwd) == tmp_path and sid == "ses-y",
    )

    assert chat_cmd._detect_session_agent(session="ses-y", project=None) == "opencode"


def test_detect_session_agent_returns_codex_when_rollout_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(chat_cmd, "_project_record", lambda project: {"name": "demo", "path": str(tmp_path)})
    monkeypatch.setattr(chat_cmd, "claude_session_exists", lambda cwd, sid: False)
    monkeypatch.setattr(chat_cmd, "_opencode_session_exists_for_scope", lambda scope, cwd, sid: False)
    monkeypatch.setattr(
        chat_cmd,
        "codex_session_exists",
        lambda cwd, sid: Path(cwd) == tmp_path and sid == "ses-z",
    )

    assert chat_cmd._detect_session_agent(session="ses-z", project=None) == "codex"


def test_detect_session_agent_returns_none_when_session_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(chat_cmd, "_project_record", lambda project: {"name": "demo", "path": str(tmp_path)})
    monkeypatch.setattr(chat_cmd, "claude_session_exists", lambda cwd, sid: False)
    monkeypatch.setattr(chat_cmd, "codex_session_exists", lambda cwd, sid: False)
    monkeypatch.setattr(chat_cmd, "_opencode_session_exists_for_scope", lambda scope, cwd, sid: False)

    assert chat_cmd._detect_session_agent(session="ses-missing", project=None) is None


def test_chat_command_auto_detects_claude_from_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _isolate_chat(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_cmd, "_detect_session_agent", lambda *, session, project: "claude")
    captured: dict[str, object] = {}

    def fake_session_runner(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(chat_cmd, "_run_mcp_agent_chat_session", fake_session_runner)

    runner = click.testing.CliRunner()
    result = runner.invoke(chat_cmd.chat, ["--session", "ses-x"], obj={})

    assert result.exit_code == 0, result.output
    assert captured["agent"] == "claude"
    assert captured["session"] == "ses-x"


def test_chat_resume_hint_for_codex_uses_latest_session_from_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    fake_calls: list[Path] = []

    def fake_latest(project_path, *, sessions_root=None):
        fake_calls.append(Path(project_path))
        return "ses_codex"

    monkeypatch.setattr(chat_cmd, "latest_codex_session_id", fake_latest)

    launch = chat_cmd._PreparedChatLaunch(
        agent="codex",
        command=["codex"],
        cwd=tmp_path,
        env={},
    )

    chat_cmd._print_codepilot_resume_hint(launch, project=None)

    assert fake_calls and fake_calls[0] == tmp_path
    output = capsys.readouterr().err
    assert "ses_codex" in output
    assert "codepilot chat --session ses_codex" in output
    assert "--agent" not in output


def test_chat_resume_hint_renders_panel_for_codex_with_requested_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    launch = chat_cmd._PreparedChatLaunch(
        agent="codex",
        command=["codex"],
        cwd=tmp_path,
        env={},
        requested_session="ses_explicit",
    )

    chat_cmd._print_codepilot_resume_hint(launch, project="demo")

    output = capsys.readouterr().err
    assert "ses_explicit" in output
    assert "codepilot chat --project demo --session ses_explicit" in output


def test_chat_resume_hint_skips_when_codex_has_no_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(chat_cmd, "latest_codex_session_id", lambda *args, **kwargs: "")

    launch = chat_cmd._PreparedChatLaunch(
        agent="codex",
        command=["codex"],
        cwd=tmp_path,
        env={},
    )

    chat_cmd._print_codepilot_resume_hint(launch, project=None)

    assert capsys.readouterr().err == ""


def test_chat_resume_hint_skips_when_claude_has_no_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(chat_cmd, "latest_claude_session_id", lambda *args, **kwargs: "")

    launch = chat_cmd._PreparedChatLaunch(
        agent="claude",
        command=["claude"],
        cwd=tmp_path,
        env={},
    )

    chat_cmd._print_codepilot_resume_hint(launch, project=None)

    assert capsys.readouterr().err == ""


def test_resolve_agent_executable_expands_path_command(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setattr(chat_cmd.shutil, "which", lambda value: "C:\\tools\\opencode.cmd" if value == "opencode" else None)

    assert chat_cmd._resolve_agent_executable("opencode", None) == "C:\\tools\\opencode.cmd"


def test_resolve_executable_path_raises_on_unknown_command(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chat_cmd.shutil, "which", lambda value: None)

    with pytest.raises(click.ClickException, match="找不到 'unknown-agent' 命令"):
        chat_cmd._resolve_executable_path("unknown-agent")


def test_resolve_executable_path_raises_on_missing_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chat_cmd.shutil, "which", lambda value: None)

    with pytest.raises(click.ClickException, match="找不到可执行文件"):
        chat_cmd._resolve_executable_path("C:\\fake\\path\\agent.exe")


def test_resolve_agent_executable_raises_when_all_fallbacks_fail(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CODEPILOT_OPENCODE_CMD", raising=False)
    monkeypatch.setattr(chat_cmd.shutil, "which", lambda value: None)

    with pytest.raises(click.ClickException, match="找不到 'opencode' 命令"):
        chat_cmd._resolve_agent_executable("opencode", None)


def test_resolve_agent_executable_uses_bundled_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("CODEPILOT_OPENCODE_CMD", raising=False)
    fake_exe = tmp_path / "bin" / "vendor"
    fake_exe.mkdir(parents=True)
    opencode_bin = fake_exe / "opencode.exe"
    opencode_bin.touch()
    monkeypatch.setattr(chat_cmd, "_find_bundled_executable", lambda agent: str(opencode_bin.resolve()) if agent == "opencode" else None)
    monkeypatch.setattr(chat_cmd.shutil, "which", lambda value: None)

    result = chat_cmd._resolve_agent_executable("opencode", None)
    assert result == str(opencode_bin.resolve())


def test_resolve_agent_executable_falls_back_when_frozen_but_no_bundled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CODEPILOT_OPENCODE_CMD", raising=False)
    monkeypatch.setattr(chat_cmd, "_find_bundled_executable", lambda agent: None)
    monkeypatch.setattr(chat_cmd.shutil, "which", lambda value: None)

    with pytest.raises(click.ClickException, match="找不到 'opencode' 命令"):
        chat_cmd._resolve_agent_executable("opencode", None)
