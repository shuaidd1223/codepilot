from __future__ import annotations

from pathlib import Path
import time

from codepilot.storage import database as db
from tests.chat_flow_testkit import register_project


def _poll_until(condition, *, timeout: float = 2.0, interval: float = 0.02) -> None:
    """Poll *condition* every *interval* seconds until it returns a truthy value or *timeout* expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return
        time.sleep(interval)


def test_webui_session_message_uses_opencode_adapter(tmp_path: Path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = db.create_session("demo", title="chat")
    calls = []

    def fake_run(project, text, *, source, external_session_id, agent=None):
        calls.append(
            {
                "project": project,
                "text": text,
                "source": source,
                "external_session_id": external_session_id,
                "agent": agent,
            }
        )
        return {
            "ok": True,
            "intent": "opencode",
            "message": "OpenCode 已处理。",
            "opencode_session_id": "ses_web",
            "tool_calls": [{"name": "codepilot.health"}],
        }

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)

    from codepilot.webapp.action_sessions import send_session_message_action

    result = send_session_message_action(session["id"], "帮我看一下任务状态", category="requirement")

    assert result["intent"] == "opencode"
    assert result["message"] == "OpenCode 已处理。"
    assert calls == [
        {
            "project": "demo",
            "text": "帮我看一下任务状态",
            "source": "web",
            "external_session_id": str(session["id"]),
            "agent": "codepilot",
        }
    ]
    messages = db.list_session_messages(session["id"])
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["intent"] == "opencode"


def test_webui_session_message_injects_runtime_config_without_rewriting_user_message(tmp_path: Path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = db.create_session("demo", title="chat")
    calls = []

    def fake_run(project, text, *, source, external_session_id, agent=None):
        calls.append({"text": text, "agent": agent})
        return {
            "ok": True,
            "intent": "opencode",
            "message": "done",
            "opencode_session_id": "ses_runtime",
            "tool_calls": [],
        }

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)

    from codepilot.webapp.action_sessions import send_session_message_action

    send_session_message_action(
        session["id"],
        "把这个需求拆成任务",
        runtime_config={
            "agentMode": "task",
        },
    )

    messages = db.list_session_messages(session["id"])
    assert messages[0]["content"] == "把这个需求拆成任务"
    assert "工作类型：创建任务" in calls[0]["text"]
    assert "用户输入：" in calls[0]["text"]
    assert calls[0]["agent"] == "codepilot"
    assert "运行位置" not in calls[0]["text"]
    assert "目标分支" not in calls[0]["text"]
    assert "模型偏好" not in calls[0]["text"]


def test_webui_session_message_threads_native_opencode_agent(tmp_path: Path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = db.create_session("demo", title="chat")
    calls = []

    def fake_run(project, text, *, source, external_session_id, agent=None):
        calls.append({"text": text, "agent": agent})
        return {
            "ok": True,
            "intent": "opencode",
            "message": "done",
            "opencode_session_id": "ses_native",
            "tool_calls": [],
        }

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message", fake_run)

    from codepilot.webapp.action_sessions import send_session_message_action

    send_session_message_action(
        session["id"],
        "只读规划一下重构思路",
        runtime_config={"agentMode": "plan"},
    )

    assert calls[0]["agent"] == "plan"
    assert calls[0]["text"] == "只读规划一下重构思路"


def test_webui_session_message_records_opencode_error(tmp_path: Path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = db.create_session("demo", title="chat")

    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda *_args, **_kwargs: {"ok": False, "message": "OpenCode 执行失败：缺少 provider"},
    )

    from codepilot.webapp.action_sessions import send_session_message_action

    result = send_session_message_action(session["id"], "你好")

    assert result["ok"] is False
    assert result["intent"] == "error"
    assert "OpenCode 执行失败" in result["message"]
    assert db.list_session_messages(session["id"])[1]["intent"] == "error"


def test_webui_session_message_async_returns_placeholder_and_finalizes(
    tmp_path: Path,
    monkeypatch,
):
    register_project(tmp_path, monkeypatch)
    session = db.create_session("demo", title="chat")
    stream_events = []

    def fake_stream(project, text, *, source, external_session_id, agent=None, on_event=None, stop_event=None):
        if on_event:
            on_event({"type": "delta", "content_delta": "处理中", "content_snapshot": "处理中"})
            on_event({"type": "tool", "tool_calls": [{"name": "codepilot.status"}]})
        stream_events.append(
            {
                "project": project,
                "text": text,
                "source": source,
                "external_session_id": external_session_id,
                "stop_event": stop_event,
            }
        )
        return {
            "ok": True,
            "intent": "opencode",
            "message": "最终回复",
            "opencode_session_id": "ses_async",
            "tool_calls": [{"name": "codepilot.status"}],
        }

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message_stream", fake_stream)

    from codepilot.webapp.action_sessions import send_session_message_action

    result = send_session_message_action(session["id"], "帮我看状态", run_async=True)

    assert result["ok"] is True
    assert result["status"] == "running"
    assert result["assistant_message_id"]
    assert result["user_message"]["content"] == "帮我看状态"
    assert result["assistant_message"]["intent"] == "streaming"

    _poll_until(lambda: len(db.list_session_messages(session["id"])) > 1 and db.list_session_messages(session["id"])[1].get("content") == "最终回复")
    messages = db.list_session_messages(session["id"])

    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["intent"] == "opencode"
    assert "ses_async" in (messages[1]["metadata"] or "")
    assert stream_events and stream_events[0]["external_session_id"] == str(session["id"])


def test_webui_session_message_async_records_stream_error(tmp_path: Path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    session = db.create_session("demo", title="chat")

    def fake_stream(*_args, **_kwargs):
        return {"ok": False, "intent": "error", "message": "OpenCode 执行失败：provider missing"}

    monkeypatch.setattr("codepilot.opencode.session.run_opencode_message_stream", fake_stream)

    from codepilot.webapp.action_sessions import send_session_message_action

    result = send_session_message_action(session["id"], "你好", run_async=True)
    assistant_id = result["assistant_message_id"]

    def _check_error_poll():
        msgs = db.list_session_messages(session["id"])
        return len(msgs) > 1 and msgs[1].get("intent") == "error" and msgs[1].get("id") == assistant_id

    _poll_until(_check_error_poll)
    updated = db.list_session_messages(session["id"])[1]
    assert "provider missing" in updated["content"]
