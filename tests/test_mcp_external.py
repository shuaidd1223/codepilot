from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.mcp.server import MCPProjectContext, create_mcp_server
from codepilot.mcp.tool_registry import default_registry
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _init_demo_project(tmp_path: Path, monkeypatch) -> Path:
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def _external_server(project_path: Path):
    import codepilot.mcp.tools.external  # noqa: F401

    return create_mcp_server(
        MCPProjectContext(project_path=project_path, project="demo"),
        registry=default_registry,
        include_health=False,
        bind_sdk=False,
    )


def _tool_by_name(name: str) -> dict[str, Any]:
    import codepilot.mcp.tools.external  # noqa: F401

    return default_registry.get(name).schema


def _error_code(payload: dict[str, Any]) -> str:
    return payload["structuredContent"]["error"]["code"]


def test_feishu_rate_limiter_allows_configured_per_minute_capacity():
    from codepilot.mcp.tools.external import PerMinuteRateLimiter

    now = [120.0]
    limiter = PerMinuteRateLimiter(limit_per_minute=2, clock=lambda: now[0])

    assert limiter.check("feishu.message.send").allowed is True
    assert limiter.check("feishu.message.send").allowed is True

    denied = limiter.check("feishu.message.send")

    assert denied.allowed is False
    assert denied.error == {
        "code": "rate_limited",
        "message": "rate limit exceeded for feishu.message.send",
        "details": {
            "key": "feishu.message.send",
            "limit_per_minute": 2,
            "retry_after_seconds": 60,
        },
    }

    now[0] = 180.0

    assert limiter.check("feishu.message.send").allowed is True


def test_feishu_rate_limit_error_response_is_mcp_tool_error_payload():
    from codepilot.mcp.tools.external import (
        PerMinuteRateLimiter,
        feishu_rate_limit_error_response,
    )

    limiter = PerMinuteRateLimiter(limit_per_minute=1, clock=lambda: 10.0)
    assert limiter.check("feishu.bot.card").allowed is True
    decision = limiter.check("feishu.bot.card")

    result = feishu_rate_limit_error_response(decision)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "rate_limited"
    assert result["structuredContent"]["error"]["details"]["limit_per_minute"] == 1


def test_external_tools_register_independent_contracts():
    import codepilot.mcp.tools.external  # noqa: F401

    expected = {"feishu_notify", "feishu_send_to_user", "webhook_invoke"}
    names = {tool.name for tool in default_registry.list()}

    assert expected <= names
    for name in expected:
        schema = _tool_by_name(name)
        assert schema["description"]
        assert schema["inputSchema"]["type"] == "object"
        assert schema["inputSchema"]["additionalProperties"] is False
        assert callable(default_registry.get(name).func)


def test_external_tool_input_schemas_capture_required_fields():
    assert _tool_by_name("feishu_notify")["inputSchema"]["required"] == [
        "project",
        "task_id",
        "task_title",
        "event",
    ]
    assert _tool_by_name("feishu_send_to_user")["inputSchema"]["required"] == [
        "user_id",
        "message",
    ]
    assert _tool_by_name("webhook_invoke")["inputSchema"]["required"] == [
        "project",
        "task_id",
        "task_title",
        "event",
    ]

    send_props = _tool_by_name("feishu_send_to_user")["inputSchema"]["properties"]
    assert send_props["user_id"] == {"type": "string"}
    assert send_props["title"] == {"type": "string", "default": "CodePilot 通知"}


def test_feishu_notify_uses_existing_feishu_api(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _external_server(project_path)
    captured: dict[str, Any] = {}

    def fake_notify(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("codepilot.feishu_bot.notify_feishu_task_event", fake_notify)

    result = server.call_tool(
        "feishu_notify",
        {
            "project": "demo",
            "task_id": 42,
            "task_title": "外部 MCP 通知",
            "event": "phase_end",
            "phase": "builder",
            "level": "info",
            "message": "builder 完成",
            "status": "in_progress",
            "summary": "ok",
            "chat_ids": ["chat-1"],
        },
    )

    assert result == {"ok": True, "sent": True, "project": "demo", "task_id": 42}
    assert captured == {
        "project_name": "demo",
        "project_path": str(project_path),
        "task_id": 42,
        "task_title": "外部 MCP 通知",
        "event": "phase_end",
        "phase": "builder",
        "level": "info",
        "message": "builder 完成",
        "status": "in_progress",
        "summary": "ok",
        "chat_ids": ["chat-1"],
    }


def test_feishu_send_to_user_uses_existing_bot_card_api(monkeypatch):
    import codepilot.mcp.tools.external.feishu_send_to_user as send_tool

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "codepilot.feishu_bot.card_builders._send_bot_card",
        lambda card, *, project_name="", chat_ids=None: calls.append(
            {"card": card, "project_name": project_name, "chat_ids": chat_ids}
        )
        or True,
    )

    result = send_tool.feishu_send_to_user(
        user_id="chat-1",
        message="外部系统需要你确认一次发布。",
        title="发布确认",
        project="demo",
    )

    assert result == {"ok": True, "sent": True, "user_id": "chat-1", "project": "demo"}
    assert calls[0]["project_name"] == "demo"
    assert calls[0]["chat_ids"] == ["chat-1"]
    assert calls[0]["card"]["header"]["title"]["content"] == "发布确认"
    assert "外部系统需要你确认一次发布。" in str(calls[0]["card"])


def test_webhook_invoke_uses_existing_webhook_notification_api(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _external_server(project_path)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "codepilot.webapp.webhook._get_webhook_config",
        lambda _path: {
            "enabled": True,
            "webhook_url": "https://example.invalid/hook",
            "provider": "generic",
            "webhook_secret": "",
        },
    )

    def fake_notify(project_path_arg, task_id, task_title, **kwargs):
        captured.update(
            {
                "project_path": project_path_arg,
                "task_id": task_id,
                "task_title": task_title,
                **kwargs,
            }
        )
        return True

    monkeypatch.setattr("codepilot.webapp.webhook.notify_task_event", fake_notify)

    result = server.call_tool(
        "webhook_invoke",
        {
            "project": "demo",
            "task_id": 7,
            "task_title": "推送外部进度",
            "event": "phase_start",
            "phase": "reviewer",
            "level": "info",
            "message": "reviewer 开始",
            "status": "in_progress",
            "summary": "",
        },
    )

    assert result == {"ok": True, "sent": True, "project": "demo", "task_id": 7}
    assert captured == {
        "project_path": str(project_path),
        "task_id": 7,
        "task_title": "推送外部进度",
        "event": "phase_start",
        "phase": "reviewer",
        "level": "info",
        "message": "reviewer 开始",
        "status": "in_progress",
        "summary": "",
    }


def test_feishu_tools_return_rate_limit_errors_without_calling_external_api(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _external_server(project_path)

    from codepilot.mcp.tools.external import PerMinuteRateLimiter
    import codepilot.mcp.tools.external.feishu_notify as notify_tool
    import codepilot.mcp.tools.external.feishu_send_to_user as send_tool

    monkeypatch.setattr(
        notify_tool,
        "FEISHU_NOTIFY_LIMITER",
        PerMinuteRateLimiter(limit_per_minute=1, clock=lambda: 120.0),
    )
    monkeypatch.setattr(
        send_tool,
        "FEISHU_SEND_LIMITER",
        PerMinuteRateLimiter(limit_per_minute=1, clock=lambda: 120.0),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "codepilot.feishu_bot.notify_feishu_task_event",
        lambda **kwargs: calls.append("notify") or True,
    )
    monkeypatch.setattr(
        "codepilot.feishu_bot.card_builders._send_bot_card",
        lambda *args, **kwargs: calls.append("send") or True,
    )

    first_notify = server.call_tool(
        "feishu_notify",
        {"project": "demo", "task_id": 1, "task_title": "一次", "event": "started"},
    )
    denied_notify = server.call_tool(
        "feishu_notify",
        {"project": "demo", "task_id": 2, "task_title": "二次", "event": "started"},
    )
    first_send = server.call_tool(
        "feishu_send_to_user",
        {"user_id": "chat-1", "message": "第一次"},
    )
    denied_send = server.call_tool(
        "feishu_send_to_user",
        {"user_id": "chat-1", "message": "第二次"},
    )

    assert first_notify["sent"] is True
    assert first_send["sent"] is True
    assert _error_code(denied_notify) == "rate_limited"
    assert _error_code(denied_send) == "rate_limited"
    assert calls == ["notify", "send"]


def test_feishu_rate_limit_does_not_block_webhook_tool(tmp_path, monkeypatch):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _external_server(project_path)

    from codepilot.mcp.tools.external import PerMinuteRateLimiter
    import codepilot.mcp.tools.external.feishu_notify as notify_tool

    monkeypatch.setattr(
        notify_tool,
        "FEISHU_NOTIFY_LIMITER",
        PerMinuteRateLimiter(limit_per_minute=1, clock=lambda: 300.0),
    )
    feishu_calls: list[str] = []
    webhook_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "codepilot.feishu_bot.notify_feishu_task_event",
        lambda **_kwargs: feishu_calls.append("notify") or True,
    )
    monkeypatch.setattr(
        "codepilot.webapp.webhook._get_webhook_config",
        lambda _path: {
            "enabled": True,
            "webhook_url": "https://example.invalid/hook",
            "provider": "generic",
            "webhook_secret": "",
        },
    )

    def fake_webhook(project_path_arg, task_id, task_title, **kwargs):
        webhook_calls.append(
            {
                "project_path": project_path_arg,
                "task_id": task_id,
                "task_title": task_title,
                **kwargs,
            }
        )
        return True

    monkeypatch.setattr("codepilot.webapp.webhook.notify_task_event", fake_webhook)

    first_notify = server.call_tool(
        "feishu_notify",
        {"project": "demo", "task_id": 1, "task_title": "一次", "event": "started"},
    )
    denied_notify = server.call_tool(
        "feishu_notify",
        {"project": "demo", "task_id": 2, "task_title": "二次", "event": "started"},
    )
    webhook_result = server.call_tool(
        "webhook_invoke",
        {"project": "demo", "task_id": 3, "task_title": "webhook", "event": "started"},
    )

    assert first_notify["sent"] is True
    assert _error_code(denied_notify) == "rate_limited"
    assert webhook_result == {"ok": True, "sent": True, "project": "demo", "task_id": 3}
    assert feishu_calls == ["notify"]
    assert webhook_calls == [
        {
            "project_path": str(project_path),
            "task_id": 3,
            "task_title": "webhook",
            "event": "started",
            "phase": "",
            "level": "info",
            "message": "",
            "status": "",
            "summary": "",
        }
    ]


def test_external_tools_return_structured_errors_for_bad_arguments_and_missing_config(
    tmp_path,
    monkeypatch,
):
    project_path = _init_demo_project(tmp_path, monkeypatch)
    server = _external_server(project_path)

    monkeypatch.setattr(
        "codepilot.webapp.webhook._get_webhook_config",
        lambda _path: {
            "enabled": False,
            "webhook_url": "",
            "provider": "auto",
            "webhook_secret": "",
        },
    )

    missing_user = server.call_tool("feishu_send_to_user", {"message": "hello"})
    blank_user = server.call_tool(
        "feishu_send_to_user",
        {"user_id": "  ", "message": "hello"},
    )
    blank_message = server.call_tool(
        "feishu_send_to_user",
        {"user_id": "chat-1", "message": "  "},
    )
    bad_task_id = server.call_tool(
        "webhook_invoke",
        {"project": "demo", "task_id": 0, "task_title": "x", "event": "started"},
    )
    missing_webhook = server.call_tool(
        "webhook_invoke",
        {"project": "demo", "task_id": 1, "task_title": "x", "event": "started"},
    )

    assert _error_code(missing_user) == "invalid_arguments"
    assert _error_code(blank_user) == "invalid_arguments"
    assert _error_code(blank_message) == "invalid_arguments"
    assert _error_code(bad_task_id) == "invalid_arguments"
    assert _error_code(missing_webhook) == "webhook_not_configured"
