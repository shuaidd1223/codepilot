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
    FakeCLIProvider,
    StreamingSchemaSubprocess,
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


def test_api_endpoint_strategy_owns_stream_fallback(monkeypatch):
    from codepilot.ai_support.providers import _APIEndpointStrategy

    calls: list[str] = []

    def _stream(_ctx):
        calls.append("stream")
        raise RuntimeError("stream down")

    def _sync(_ctx):
        calls.append("sync")
        return "sync fallback"

    monkeypatch.setattr("codepilot.ai_support.providers._should_stream_llm_progress", lambda: True)

    strategy = _APIEndpointStrategy(
        endpoint="fake",
        sync_runner=_sync,
        stream_runner=_stream,
    )
    text = strategy.run(SimpleNamespace(provider=SimpleNamespace(name="fake-provider")))

    assert text == "sync fallback"
    assert calls == ["stream", "sync"]


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


def test_claude_schema_prompt_streams_stdout_chunks_before_json_parse(monkeypatch):
    from codepilot.ai_support import service as service_mod

    fake_subprocess = StreamingSchemaSubprocess(
        [b'{"intent":', b' "task", "reason": "streamed"}']
    )
    streamed: list[str] = []

    monkeypatch.setattr(
        service_mod,
        "resolve_cli_provider",
        lambda *_args, **_kwargs: SimpleNamespace(
            name="Claude Code",
            find_executable=lambda: "claude",
        ),
    )
    monkeypatch.setattr(service_mod, "subprocess", fake_subprocess)

    result = service_mod._run_claude_schema_prompt(
        "plan",
        STRUCTURED_SCHEMA,
        planner="claude",
        timeout=2,
        stream_callback=streamed.append,
    )

    assert result == {"intent": "task", "reason": "streamed"}
    assert streamed
    assert "}" not in streamed[0]
    assert "".join(streamed) == '{"intent": "task", "reason": "streamed"}'
    assert fake_subprocess.process is not None


def test_codex_schema_prompt_streams_stdout_chunks_and_keeps_sync_result(monkeypatch):
    from codepilot.ai_support import service as service_mod

    fake_subprocess = StreamingSchemaSubprocess(
        [b'{"intent":', b' "requirement", "reason": "codex streamed"}']
    )
    streamed: list[str] = []

    monkeypatch.setattr(
        service_mod,
        "check_provider_availability",
        lambda *_args, **_kwargs: (True, "ok"),
    )
    monkeypatch.setattr(
        service_mod,
        "resolve_cli_provider",
        lambda *_args, **_kwargs: FakeCLIProvider(exe="codex"),
    )
    monkeypatch.setattr(service_mod, "subprocess", fake_subprocess)

    result = service_mod._run_codex_schema_prompt(
        "plan",
        STRUCTURED_SCHEMA,
        timeout=2,
        stream_callback=streamed.append,
    )

    assert result == {"intent": "requirement", "reason": "codex streamed"}
    assert streamed
    assert "}" not in streamed[0]
    assert "".join(streamed) == '{"intent": "requirement", "reason": "codex streamed"}'
    assert fake_subprocess.last_stdin is not None
    assert fake_subprocess.last_stdin.data == b"plan"


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
def test_call_structured_route_matrix(tmp_path, monkeypatch, gateway_state, provider_key, provider_kwargs, request_kwargs, expected):
    provider = FakeAPIProvider(**provider_kwargs)
    gateway_state["registry"][provider_key] = provider
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global.toml"))
    resolved_request_kwargs = dict(request_kwargs)
    resolved_request_kwargs.setdefault("project_path", str(tmp_path))

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            **resolved_request_kwargs,
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


def test_call_structured_marks_unavailable_api_before_cli_fallback(gateway_state, monkeypatch):
    marks: list[dict[str, object]] = []
    provider = FakeAPIProvider(needs_key=False, raises=True)
    gateway_state["registry"]["localcustom"] = provider

    def _mark(provider_key, provider_obj, reason, **kwargs):
        marks.append(
            {
                "provider_key": provider_key,
                "provider": provider_obj,
                "reason": reason,
                "source": kwargs.get("source"),
                "project_path": kwargs.get("project_path"),
            }
        )

    monkeypatch.setattr("codepilot.gateway.api.mark_provider_unavailable", _mark)

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            classifier_provider="localcustom",
            planner="codex",
            project_path="D:/project",
        )
    )

    assert resp.ok is True
    assert resp.source == "cli:codex"
    assert marks == [
        {
            "provider_key": "localcustom",
            "provider": provider,
            "reason": "boom",
            "source": "gateway",
            "project_path": "D:/project",
        }
    ]


def test_call_structured_falls_back_to_cli_when_api_returns_non_json(gateway_state, monkeypatch):
    provider = FakeAPIProvider(needs_key=True, api_key="sk-test")
    gateway_state["registry"]["openai"] = provider
    monkeypatch.setattr(
        "codepilot.ai_support.providers._run_api_provider",
        lambda *_args, **_kwargs: "not json",
    )

    resp = ai_gateway.call_structured(
        GatewayRequest(
            prompt="hi",
            schema=STRUCTURED_SCHEMA,
            classifier_provider="openai",
            planner="claude",
            project_path="D:/project",
            config_ref="D:/config/AGENTS.toml",
        )
    )

    assert resp.ok is True
    assert resp.source == "cli:claude"
    assert resp.payload == {"intent": "requirement", "reason": "from claude CLI"}
    assert gateway_state["cli_calls"][0]["kwargs"]["project_path"] == "D:/project"
    assert gateway_state["cli_calls"][0]["kwargs"]["config_ref"] == "D:/config/AGENTS.toml"


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


def test_call_text_marks_api_failure_then_runs_project_codex_cli(gateway_state, monkeypatch):
    marks: list[dict[str, object]] = []
    provider = FakeAPIProvider(needs_key=False, raises=True)
    gateway_state["registry"]["localcustom"] = provider

    def _fake_resolve_cli_provider(cli_name, provider_ref):
        assert provider_ref == "D:/config/AGENTS.toml"
        if cli_name == "claude":
            return FakeCLIProvider(exe="")
        return FakeCLIProvider(exe="C:/bin/codex.exe")

    calls: list[dict[str, object]] = []

    def _fake_subprocess_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return CompletedProcessStub(returncode=0, stdout=" project answer ", stderr="")

    monkeypatch.setattr("codepilot.ai_support.providers.resolve_cli_provider", _fake_resolve_cli_provider)
    monkeypatch.setattr("codepilot.gateway.execute.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr(
        "codepilot.gateway.api.mark_provider_unavailable",
        lambda provider_key, provider_obj, reason, **kwargs: marks.append(
            {
                "provider_key": provider_key,
                "provider": provider_obj,
                "reason": reason,
                "source": kwargs.get("source"),
                "project_path": kwargs.get("project_path"),
            }
        ),
    )

    resp = ai_gateway.call_text(
        GatewayRequest(
            prompt="hi",
            classifier_provider="localcustom",
            planner="codex",
            project_path="D:/project",
            config_ref="D:/config/AGENTS.toml",
            timeout=12,
        )
    )

    assert resp.ok is True
    assert resp.source == "cli:codex"
    assert resp.text == "project answer"
    assert marks == [
        {
            "provider_key": "localcustom",
            "provider": provider,
            "reason": "boom",
            "source": "gateway",
            "project_path": "D:/project",
        }
    ]
    assert calls[0]["cmd"][:3] == ["C:/bin/codex.exe", "-C", "D:/project"]
    assert calls[0]["input"] == b"hi"
    assert calls[0]["timeout"] == 12

