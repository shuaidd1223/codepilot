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


class _StreamingBytes:
    def __init__(self, chunks):
        self._chunks = [chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8") for chunk in chunks]
        self._index = 0
        self._offset = 0

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._chunks)

    def read(self, size=-1):
        if self.exhausted:
            return b""
        if size is None or size < 0:
            remaining = self._chunks[self._index :]
            self._index = len(self._chunks)
            self._offset = 0
            return b"".join(remaining)

        current = self._chunks[self._index]
        end = min(self._offset + size, len(current))
        piece = current[self._offset : end]
        self._offset = end
        if self._offset >= len(current):
            self._index += 1
            self._offset = 0
        return piece

    def __iter__(self):
        while not self.exhausted:
            yield self.read()


class _WritableBytes:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    def close(self):
        self.closed = True


class StreamingSchemaSubprocess:
    """Subprocess stub whose stdout arrives in caller-provided chunks."""

    DEVNULL = -3
    PIPE = -1

    class TimeoutExpired(Exception):
        def __init__(self, cmd, timeout):
            super().__init__(f"timeout {timeout} for {cmd!r}")
            self.cmd = cmd
            self.timeout = timeout

    def __init__(self, stdout_chunks, *, returncode: int = 0):
        self.stdout_chunks = stdout_chunks
        self.returncode = returncode
        self.last_cmd = None
        self.last_stdin = None
        self.process = None

    def Popen(self, cmd, **_kwargs):
        stdout = _StreamingBytes(self.stdout_chunks)
        process = type("StreamingProcess", (), {})()
        process.pid = 1234
        process.stdout = stdout
        process.stderr = _StreamingBytes([])
        process.stdin = _WritableBytes()
        process.returncode = self.returncode

        def _poll():
            if not stdout.exhausted:
                return None
            return process.returncode

        def _wait(timeout=None):
            return process.returncode

        process.poll = _poll
        process.wait = _wait
        self.last_cmd = list(cmd)
        self.last_stdin = process.stdin
        self.process = process
        return process


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

    monkeypatch.setattr("codepilot.ai_support.providers._run_api_provider", _fake_run_api_provider)
    monkeypatch.setattr("codepilot.ai_support.service._run_claude_schema_prompt", _fake_run_claude_schema_prompt)
    monkeypatch.setattr("codepilot.ai_support.service._run_codex_schema_prompt", _fake_run_codex_schema_prompt)

    fake_registry = {}
    monkeypatch.setattr("codepilot.ai_support.providers.API_PROVIDERS", fake_registry)

    return {
        "api_calls": api_calls,
        "api_providers": api_providers,
        "cli_calls": cli_calls,
        "registry": fake_registry,
    }
