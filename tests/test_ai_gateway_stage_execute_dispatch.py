from __future__ import annotations

from types import SimpleNamespace

import pytest

from codepilot.gateway import service as ai_gateway
from codepilot.core import progress_bus
from codepilot.gateway.service import GatewayRequest
from codepilot.gateway.execute import (
    execute_api_prompt,
    execute_structured_cli_call,
    execute_text_cli_candidate,
)
from codepilot.gateway.resolution import (
    ResolvedStructuredCLICall,
    ResolvedTextCLICandidate,
)
from tests.ai_gateway_testkit import (
    CompletedProcessStub,
    FakeAPIProvider,
    STRUCTURED_SCHEMA,
    gateway_state,
)


def test_execute_api_prompt_delegates_to_provider_runner(monkeypatch):
    captured = {}

    def _fake_run(provider, prompt):
        captured["provider"] = provider
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr("codepilot.ai_support.providers._run_api_provider", _fake_run)

    provider = object()
    raw = execute_api_prompt(provider, "hello")

    assert raw == "ok"
    assert captured["provider"] is provider
    assert captured["prompt"] == "hello"


def test_run_api_provider_streams_heartbeat_events_when_subscribed():
    from codepilot.ai_support.providers import _run_api_provider

    class _FakeChatCompletions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="hello"))]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=" world"))]
                ),
            ]

    provider = SimpleNamespace(
        name="fake-provider",
        model="fake-model",
        max_tokens=128,
        temperature=0.1,
        build_client=lambda: (
            SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions())),
            "chat.completions",
        ),
    )

    progress_bus.clear_subscribers_for_tests()
    events: list[dict] = []
    with progress_bus.subscription(events.append):
        with progress_bus.llm_context(task_id=42, stage="planner", label="意图分类"):
            text = _run_api_provider(provider, "hello")

    assert text == "hello world"
    heartbeat_events = [event for event in events if event["extra"].get("llm_heartbeat")]
    assert heartbeat_events
    assert heartbeat_events[0]["task_id"] == 42
    assert heartbeat_events[0]["stage"] == "planner"
    assert any(event["level"] == "heartbeat" for event in heartbeat_events)
    assert heartbeat_events[0]["type"] == "phase_start"
    assert any(event["type"] == "heartbeat" for event in heartbeat_events)
    assert any(event["type"] == "phase_end" for event in heartbeat_events)
    assert any(event["extra"].get("final") is True for event in heartbeat_events)


def test_run_api_provider_falls_back_to_openai_sync_when_streaming_fails():
    from codepilot.ai_support.providers import _run_api_provider

    calls: list[dict] = []

    class _FakeChatCompletions:
        def create(self, **kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("stream"):
                raise RuntimeError("stream down")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="sync fallback"))]
            )

    provider = SimpleNamespace(
        name="fake-provider",
        model="fake-model",
        max_tokens=128,
        temperature=0.1,
        build_client=lambda: (
            SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions())),
            "chat.completions",
        ),
    )

    progress_bus.clear_subscribers_for_tests()
    with progress_bus.subscription(lambda _event: None):
        text = _run_api_provider(provider, "hello")

    assert text == "sync fallback"
    assert [call.get("stream", False) for call in calls] == [True, False]


def test_run_api_provider_routes_anthropic_streaming_events():
    from codepilot.ai_support.providers import _run_api_provider

    captured: list[dict] = []

    class _FakeMessages:
        def create(self, **kwargs):
            captured.append(dict(kwargs))
            assert kwargs["stream"] is True
            return [
                SimpleNamespace(
                    type="content_block_start",
                    content_block=SimpleNamespace(text="hello"),
                ),
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(text=" world"),
                ),
            ]

    provider = SimpleNamespace(
        name="fake-provider",
        model="fake-model",
        max_tokens=128,
        temperature=0.1,
        build_client=lambda: (
            SimpleNamespace(messages=_FakeMessages()),
            "messages",
        ),
    )

    progress_bus.clear_subscribers_for_tests()
    with progress_bus.subscription(lambda _event: None):
        text = _run_api_provider(provider, "hello", system_prompt="be brief")

    assert text == "hello world"
    assert captured[0]["system"] == "be brief"
    assert captured[0]["messages"] == [{"role": "user", "content": "hello"}]


def test_execute_structured_cli_call_dispatches_claude_variant(monkeypatch):
    captured = {}

    def _fake_claude(prompt, schema, **kwargs):
        captured["prompt"] = prompt
        captured["schema"] = schema
        captured["kwargs"] = kwargs
        return {"intent": "task"}

    monkeypatch.setattr("codepilot.ai_support.service._run_claude_schema_prompt", _fake_claude)

    request = GatewayRequest(
        prompt="plan",
        schema={"type": "object"},
        project_path="D:/demo/project",
        config_ref="D:/demo/config/AGENTS.toml",
        timeout=42,
    )
    resolved = ResolvedStructuredCLICall(
        cli_name="claude",
        planner="claude-sonnet",
        source="cli:claude",
    )

    payload = execute_structured_cli_call(request, resolved)

    assert payload == {"intent": "task"}
    assert captured["prompt"] == "plan"
    assert captured["schema"] == {"type": "object"}
    assert captured["kwargs"]["planner"] == "claude-sonnet"
    assert captured["kwargs"]["project_path"] == "D:/demo/project"
    assert captured["kwargs"]["config_ref"] == "D:/demo/config/AGENTS.toml"
    assert captured["kwargs"]["timeout"] == 42


def test_execute_text_cli_candidate_reports_success(monkeypatch):
    monkeypatch.setattr(
        "codepilot.gateway.execute.subprocess.run",
        lambda *args, **kwargs: CompletedProcessStub(returncode=0, stdout="  answer  ", stderr=""),
    )

    ok, text, error = execute_text_cli_candidate(
        GatewayRequest(prompt="hi", timeout=10),
        ResolvedTextCLICandidate(
            cli_name="codex",
            source="cli:codex",
            cmd=["codex", "exec", "-"],
        ),
    )

    assert ok is True
    assert text == "answer"
    assert error == ""


def test_execute_text_cli_candidate_reports_failure(monkeypatch):
    monkeypatch.setattr(
        "codepilot.gateway.execute.subprocess.run",
        lambda *args, **kwargs: CompletedProcessStub(returncode=1, stdout="", stderr="boom"),
    )

    ok, text, error = execute_text_cli_candidate(
        GatewayRequest(prompt="hi", timeout=10),
        ResolvedTextCLICandidate(
            cli_name="claude",
            source="cli:claude",
            cmd=["claude", "-p"],
        ),
    )

    assert ok is False
    assert text == ""
    assert error == "claude exit=1 stderr=boom"


def test_execute_text_cli_candidate_decodes_non_utf8_stderr(monkeypatch):
    monkeypatch.setattr(
        "codepilot.gateway.execute.subprocess.run",
        lambda *args, **kwargs: CompletedProcessStub(
            returncode=1,
            stdout=b"",
            stderr="系统繁忙，请稍后重试".encode("gb18030"),
        ),
    )

    ok, text, error = execute_text_cli_candidate(
        GatewayRequest(prompt="hi", timeout=10),
        ResolvedTextCLICandidate(
            cli_name="claude",
            source="cli:claude",
            cmd=["claude", "-p"],
        ),
    )

    assert ok is False
    assert text == ""
    assert "系统繁忙" in error


@pytest.mark.parametrize(
    ("provider_key", "provider_kwargs", "request_kwargs", "expected"),
    [
        (
            "openai",
            {"needs_key": True, "api_key": "sk-test"},
            {"classifier_provider": "openai", "api_key": "sk-test", "planner": "claude"},
            {"source": "api:openai", "api_calls": 1, "cli_calls": 0, "cli_name": ""},
        ),
        (
            "openai",
            {"needs_key": True, "api_key": ""},
            {"classifier_provider": "openai", "planner": "claude"},
            {"source": "cli:claude", "api_calls": 0, "cli_calls": 1, "cli_name": "claude"},
        ),
        (
            "localcustom",
            {"needs_key": False, "raises": True},
            {"classifier_provider": "localcustom", "planner": "codex"},
            {"source": "cli:codex", "api_calls": 1, "cli_calls": 1, "cli_name": "codex"},
        ),
    ],
    ids=["api-success", "missing-key-cli-fallback", "api-error-cli-fallback"],
)
def test_call_structured_route_matrix(gateway_state, provider_key, provider_kwargs, request_kwargs, expected):
    provider = FakeAPIProvider(**provider_kwargs)
    gateway_state["registry"][provider_key] = provider

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            **request_kwargs,
        )
    )

    assert resp.ok is True
    assert resp.source == expected["source"]
    assert len(gateway_state["api_calls"]) == expected["api_calls"]
    assert len(gateway_state["cli_calls"]) == expected["cli_calls"]
    if expected["cli_name"]:
        assert gateway_state["cli_calls"][0]["cli"] == expected["cli_name"]
    if expected["source"].startswith("api:"):
        assert resp.payload == {"intent": "task", "reason": "from api"}


def test_call_structured_passes_config_ref_to_cli(gateway_state):
    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            planner="claude",
            project_path="C:/project",
            config_ref="C:/config-root/AGENTS.toml",
        )
    )

    assert resp.ok is True
    assert resp.source == "cli:claude"
    assert gateway_state["cli_calls"][0]["kwargs"]["project_path"] == "C:/project"
    assert gateway_state["cli_calls"][0]["kwargs"]["config_ref"] == "C:/config-root/AGENTS.toml"


def test_call_text_prefers_api_when_key_available(gateway_state):
    provider = FakeAPIProvider(needs_key=True, api_key="sk-test")
    gateway_state["registry"]["openai"] = provider

    resp = ai_gateway.call_text(
        GatewayRequest(
            prompt="hi",
            classifier_provider="openai",
            api_key="sk-test",
            planner="claude",
        )
    )

    assert resp.ok is True
    assert resp.source == "api:openai"
    assert resp.text == '{"intent": "task", "reason": "from api"}'
    assert gateway_state["cli_calls"] == []

