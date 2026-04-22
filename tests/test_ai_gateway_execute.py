from __future__ import annotations

from codepilot.ai_gateway_execute import (
    execute_api_prompt,
    execute_structured_cli_call,
    execute_text_cli_candidate,
)
from codepilot.ai_gateway_resolution import (
    ResolvedStructuredCLICall,
    ResolvedTextCLICandidate,
)
from codepilot.ai_gateway_types import GatewayRequest
from tests.ai_gateway_testkit import CompletedProcessStub


def test_execute_api_prompt_delegates_to_provider_runner(monkeypatch):
    captured = {}

    def _fake_run(provider, prompt):
        captured["provider"] = provider
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr("codepilot.ai_providers._run_api_provider", _fake_run)

    provider = object()
    raw = execute_api_prompt(provider, "hello")

    assert raw == "ok"
    assert captured["provider"] is provider
    assert captured["prompt"] == "hello"


def test_execute_structured_cli_call_dispatches_claude_variant(monkeypatch):
    captured = {}

    def _fake_claude(prompt, schema, **kwargs):
        captured["prompt"] = prompt
        captured["schema"] = schema
        captured["kwargs"] = kwargs
        return {"intent": "task"}

    monkeypatch.setattr("codepilot.ai._run_claude_schema_prompt", _fake_claude)

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
        "codepilot.ai_gateway_execute.subprocess.run",
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
        "codepilot.ai_gateway_execute.subprocess.run",
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
        "codepilot.ai_gateway_execute.subprocess.run",
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
