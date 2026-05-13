from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from codepilot.core.config import AgentsConfig
from codepilot.storage import database as db
from tests.chat_flow_testkit import register_project


def _isolate_opencode_runtime(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "codepilot-home"
    monkeypatch.setattr("codepilot.opencode.paths.global_storage_root", lambda: root)
    return root


def test_run_opencode_message_starts_new_json_session(tmp_path: Path, monkeypatch):
    runtime_root = _isolate_opencode_runtime(tmp_path, monkeypatch)
    project_path = register_project(tmp_path, monkeypatch)
    calls = []

    def fake_run(command, *, cwd, env, capture_output, timeout, **_kwargs):
        calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": env,
                "capture_output": capture_output,
                "timeout": timeout,
            }
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "session.updated", "sessionID": "ses_123"}),
                json.dumps({"type": "message.part", "role": "assistant", "text": "已经查看项目状态。"}),
            ]
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message(
        "demo",
        "看一下当前状态",
        source="feishu",
        external_session_id="chat-a",
    )

    assert result["ok"] is True
    assert result["opencode_session_id"] == "ses_123"
    assert result["message"] == "已经查看项目状态。"
    assert calls[0]["command"][1:6] == ["run", "--agent", "codepilot", "--format", "json"]
    assert "--title" in calls[0]["command"]
    assert calls[0]["command"][-1] == "看一下当前状态"
    assert calls[0]["timeout"] > 0
    assert calls[0]["cwd"] == str(project_path.resolve())
    assert Path(calls[0]["env"]["OPENCODE_CONFIG"]).is_relative_to(runtime_root)
    assert Path(calls[0]["env"]["XDG_DATA_HOME"]).is_relative_to(runtime_root)
    assert Path(calls[0]["env"]["XDG_CACHE_HOME"]).is_relative_to(runtime_root)
    assert Path(calls[0]["env"]["XDG_STATE_HOME"]).is_relative_to(runtime_root)
    assert not (project_path / ".codepilot" / "opencode").exists()

    state = db.get_service_state("opencode_chat", "feishu:chat-a:demo")
    assert state and state["meta"]["opencode_session_id"] == "ses_123"


def test_run_opencode_message_normalizes_project_alias_for_mcp_and_session_scope(
    tmp_path: Path,
    monkeypatch,
):
    runtime_root = _isolate_opencode_runtime(tmp_path, monkeypatch)
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    db.init_db()
    project_path = tmp_path / "flower"
    project_path.mkdir()
    db.register_project("codepilot-dev", str(project_path))
    monkeypatch.chdir(project_path)
    calls = []

    def fake_run(command, *, cwd, env, capture_output, timeout, **_kwargs):
        calls.append({"command": command, "cwd": cwd, "env": env})
        stdout = "\n".join(
            [
                json.dumps({"type": "session.updated", "sessionID": "ses_alias"}),
                json.dumps({"type": "message.part", "role": "assistant", "text": "收到。"}),
            ]
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message(
        "flower",
        "看一下当前状态",
        source="feishu",
        external_session_id="chat-a",
    )

    assert result["ok"] is True
    assert result["project"] == "codepilot-dev"
    config_path = Path(calls[0]["env"]["OPENCODE_CONFIG"])
    assert config_path.is_relative_to(runtime_root)
    assert not (project_path / ".codepilot" / "opencode").exists()
    opencode_config = json.loads(config_path.read_text(encoding="utf-8"))
    mcp_command = opencode_config["mcp"]["codepilot"]["command"]
    assert mcp_command[mcp_command.index("--project") + 1] == "codepilot-dev"
    assert db.get_service_state("opencode_chat", "feishu:chat-a:codepilot-dev")


def test_run_opencode_message_uses_tool_level_profile_not_project_opencode_config(
    tmp_path: Path,
    monkeypatch,
):
    runtime_root = _isolate_opencode_runtime(tmp_path, monkeypatch)
    project_path = register_project(tmp_path, monkeypatch)
    project_cfg = AgentsConfig.from_dict(
        {
            "opencode": {
                "profile": {"brand_name": "ProjectBrand", "default_agent": "project-agent"},
                "agent": {"name": "project-agent", "prompt": "Project prompt."},
            }
        }
    )
    monkeypatch.setattr("codepilot.opencode.session.load_project_config", lambda project_path: project_cfg)
    calls = []

    def fake_run(command, *, cwd, env, capture_output, timeout, **_kwargs):
        calls.append({"command": command, "cwd": cwd, "env": env})
        stdout = json.dumps({"type": "message.part", "role": "assistant", "text": "收到。"})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message("demo", "看一下当前状态", source="web", external_session_id="1")

    assert result["ok"] is True
    assert calls[0]["command"][calls[0]["command"].index("--agent") + 1] == "codepilot"
    config_path = Path(calls[0]["env"]["OPENCODE_CONFIG"])
    assert config_path.is_relative_to(runtime_root)
    assert not (project_path / ".codepilot" / "opencode").exists()
    opencode_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert opencode_config["default_agent"] == "codepilot"
    assert "ProjectBrand" not in config_path.read_text(encoding="utf-8")
    assert calls[0]["cwd"] == str(project_path.resolve())


def test_run_opencode_message_maps_deepseek_to_custom_provider_env(
    tmp_path: Path,
    monkeypatch,
):
    _isolate_opencode_runtime(tmp_path, monkeypatch)
    register_project(tmp_path, monkeypatch)
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
    monkeypatch.setattr("codepilot.opencode.session.load_project_config", lambda project_path: project_cfg)
    calls = []

    def fake_run(command, *, cwd, env, capture_output, timeout, **_kwargs):
        calls.append({"command": command, "cwd": cwd, "env": env})
        stdout = json.dumps({"type": "message.part", "role": "assistant", "text": "收到。"})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message("demo", "你好", source="web", external_session_id="1")

    assert result["ok"] is True
    assert calls[0]["env"]["DEEPSEEK_API_KEY"] == "sk-ds-test"
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert "OPENAI_BASE_URL" not in calls[0]["env"]
    assert "DEEPSEEK_BASE_URL" not in calls[0]["env"]


def test_run_opencode_message_continues_existing_session(tmp_path: Path, monkeypatch):
    _isolate_opencode_runtime(tmp_path, monkeypatch)
    register_project(tmp_path, monkeypatch)
    db.upsert_service_state(
        "opencode_chat",
        "web:42:demo",
        status="active",
        meta={"opencode_session_id": "ses_existing", "source": "web", "project": "demo"},
    )
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        stdout = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "继续处理。"}})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message("demo", "继续", source="web", external_session_id="42")

    assert result["ok"] is True
    assert "--session" in calls[0]
    assert calls[0][calls[0].index("--session") + 1] == "ses_existing"
    assert "--title" not in calls[0]
    assert result["message"] == "继续处理。"


def test_run_opencode_message_reports_failure_in_chinese(tmp_path: Path, monkeypatch):
    _isolate_opencode_runtime(tmp_path, monkeypatch)
    register_project(tmp_path, monkeypatch)

    def fake_run(command, **_kwargs):
        return SimpleNamespace(returncode=7, stdout=b"", stderr=b"provider missing")

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message("demo", "你好", source="feishu", external_session_id="chat-b")

    assert result["ok"] is False
    assert result["intent"] == "error"
    assert "OpenCode 执行失败" in result["message"]
    assert "provider missing" in result["message"]


def test_run_opencode_message_decodes_cp936_stderr_on_windows(tmp_path: Path, monkeypatch):
    """Windows shim/cmd.exe errors are emitted in cp936 (GBK); we must surface them
    as readable Chinese instead of U+FFFD streams."""
    _isolate_opencode_runtime(tmp_path, monkeypatch)
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr("codepilot.opencode.session.sys.platform", "win32")
    cn_error = "找不到指定的文件。".encode("cp936")

    def fake_run(command, **_kwargs):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=cn_error)

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message("demo", "你好", source="feishu", external_session_id="chat-cp936")

    assert result["ok"] is False
    assert "找不到指定的文件" in result["message"]
    assert "�" not in result["message"]


def test_run_opencode_message_clears_stale_session_when_continuation_fails(tmp_path: Path, monkeypatch):
    """If --session <id> fails and OpenCode never returns a new session id, the
    saved opencode_session_id is treated as stale and cleared so the next call
    starts fresh."""
    _isolate_opencode_runtime(tmp_path, monkeypatch)
    register_project(tmp_path, monkeypatch)
    db.upsert_service_state(
        "opencode_chat",
        "web:99:demo",
        status="active",
        meta={"opencode_session_id": "ses_stale", "source": "web", "project": "demo"},
    )

    def fake_run(command, **_kwargs):
        assert "--session" in command
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"session not found")

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message("demo", "继续", source="web", external_session_id="99")

    assert result["ok"] is False
    assert "已自动清理失效的 OpenCode 会话引用" in result["message"]
    state = db.get_service_state("opencode_chat", "web:99:demo") or {}
    meta = state.get("meta") or {}
    assert not meta.get("opencode_session_id")
    assert state.get("status") == "stale"


def test_run_opencode_message_reports_timeout_in_chinese(tmp_path: Path, monkeypatch):
    _isolate_opencode_runtime(tmp_path, monkeypatch)
    register_project(tmp_path, monkeypatch)

    def fake_run(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, timeout=1, output="partial stdout", stderr="still waiting")

    monkeypatch.setattr("codepilot.opencode.session.subprocess.run", fake_run)

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message("demo", "你好", source="feishu", external_session_id="chat-timeout", timeout_seconds=1)

    assert result["ok"] is False
    assert result["intent"] == "error"
    assert "OpenCode 执行超时" in result["message"]
    assert "1" in result["message"]


def test_run_opencode_message_stream_emits_deltas_tools_and_final_result(
    tmp_path: Path,
    monkeypatch,
):
    _isolate_opencode_runtime(tmp_path, monkeypatch)
    register_project(tmp_path, monkeypatch)
    events = []
    popen_calls = []

    class FakeStdout:
        def __init__(self, lines):
            self.lines = list(lines)

        def readline(self):
            if not self.lines:
                return ""
            return self.lines.pop(0)

    class FakeProcess:
        def __init__(self, command, **kwargs):
            popen_calls.append({"command": command, **kwargs})
            self.returncode = 0
            self.stdout = FakeStdout(
                [
                    json.dumps({"type": "session.updated", "sessionID": "ses_stream"}) + "\n",
                    json.dumps({"type": "message.part", "role": "assistant", "text": "第一段"}) + "\n",
                    json.dumps({"type": "tool.call", "tool": "codepilot.status"}) + "\n",
                    json.dumps({"type": "message.part", "role": "assistant", "text": "第二段"}) + "\n",
                ]
            )
            self.stderr = SimpleNamespace(read=lambda: "")

        def poll(self):
            return 0 if not self.stdout.lines else None

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

    monkeypatch.setattr("codepilot.opencode.session.subprocess.Popen", FakeProcess)

    from codepilot.opencode.session import run_opencode_message_stream

    result = run_opencode_message_stream(
        "demo",
        "流式回复",
        source="web",
        external_session_id="1",
        on_event=events.append,
    )

    assert result["ok"] is True
    assert result["opencode_session_id"] == "ses_stream"
    assert result["message"] == "第一段第二段"
    assert result["tool_calls"] == [{"name": "codepilot.status"}]
    assert [event["type"] for event in events] == ["started", "delta", "tool", "delta", "done"]
    assert events[1]["content_delta"] == "第一段"
    assert events[-1]["content_snapshot"] == "第一段第二段"
    assert popen_calls[0]["command"][1:6] == ["run", "--agent", "codepilot", "--format", "json"]


def test_run_opencode_message_stream_can_be_cancelled(tmp_path: Path, monkeypatch):
    _isolate_opencode_runtime(tmp_path, monkeypatch)
    register_project(tmp_path, monkeypatch)
    stop_event = threading.Event()

    class FakeStdout:
        def __init__(self):
            self.lines = [json.dumps({"type": "message.part", "role": "assistant", "text": "部分"}) + "\n"]

        def readline(self):
            if self.lines:
                stop_event.set()
                return self.lines.pop(0)
            return ""

    class FakeProcess:
        terminated = False

        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.stdout = FakeStdout()
            self.stderr = SimpleNamespace(read=lambda: "")

        def poll(self):
            return None if self.returncode is None else self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = -15
            return self.returncode

        def terminate(self):
            FakeProcess.terminated = True
            self.returncode = -15

    monkeypatch.setattr("codepilot.opencode.session.subprocess.Popen", FakeProcess)

    from codepilot.opencode.session import run_opencode_message_stream

    result = run_opencode_message_stream(
        "demo",
        "取消",
        source="web",
        external_session_id="1",
        stop_event=stop_event,
    )

    assert result["ok"] is False
    assert result["intent"] == "cancelled"
    assert "已停止" in result["message"]
    assert FakeProcess.terminated is True
