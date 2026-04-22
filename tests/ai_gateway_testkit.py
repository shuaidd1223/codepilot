"""Shared fixtures/test doubles for AI gateway tests."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

STRUCTURED_SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}}}


@dataclass
class FakeAPIProvider:
    """Dataclass stand-in for ``ai_providers.APIProvider``."""

    name: str = "fake-provider"
    model: str = "fake-model"
    api_key: str = ""
    base_url: str = ""
    needs_key: bool = True
    raises: bool = False

    def requires_api_key(self) -> bool:
        return self.needs_key

    def resolve_api_key(self) -> str:
        return self.api_key


@dataclass
class FakeCLIProvider:
    exe: str = ""

    def find_executable(self) -> str:
        return self.exe


@dataclass
class CompletedProcessStub:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def build_gateway_capture(*, answer_text: str):
    captured: dict[str, dict] = {}

    def _classify(_text, **kwargs):
        captured["classify"] = kwargs
        return {"intent": "question", "reason": "test", "source": "test"}

    def _answer(**kwargs):
        captured["answer"] = kwargs
        return answer_text

    return captured, _classify, _answer


@pytest.fixture
def gateway_state(monkeypatch):
    """Replace gateway API/CLI runners with scripted fakes."""
    api_calls: list[str] = []
    api_providers: list[FakeAPIProvider] = []
    cli_calls: list[dict] = []

    def _fake_run_api_provider(provider, prompt):
        api_calls.append(prompt)
        api_providers.append(provider)
        if getattr(provider, "raises", False):
            raise RuntimeError("boom")
        return json.dumps({"intent": "task", "reason": "from api"})

    def _fake_run_claude_schema_prompt(prompt, schema, **kw):
        cli_calls.append({"cli": "claude", "prompt": prompt, "schema": schema, "kwargs": kw})
        return {"intent": "requirement", "reason": "from claude CLI"}

    def _fake_run_codex_schema_prompt(prompt, schema, **kw):
        cli_calls.append({"cli": "codex", "prompt": prompt, "schema": schema, "kwargs": kw})
        return {"intent": "requirement", "reason": "from codex CLI"}

    monkeypatch.setattr("codepilot.ai_providers._run_api_provider", _fake_run_api_provider)
    monkeypatch.setattr("codepilot.ai._run_claude_schema_prompt", _fake_run_claude_schema_prompt)
    monkeypatch.setattr("codepilot.ai._run_codex_schema_prompt", _fake_run_codex_schema_prompt)

    fake_registry = {}
    monkeypatch.setattr("codepilot.ai_providers.API_PROVIDERS", fake_registry)

    return {
        "api_calls": api_calls,
        "api_providers": api_providers,
        "cli_calls": cli_calls,
        "registry": fake_registry,
    }
