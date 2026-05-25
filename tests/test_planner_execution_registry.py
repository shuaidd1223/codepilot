"""Unit tests for the shared schema-prompt subprocess runner (task #212).

After the extraction, ``run_claude_schema_prompt`` / ``run_codex_schema_prompt``
/ ``run_opencode_schema_prompt`` are thin wrappers around one internal
``_run_family_schema_prompt`` core. These tests:

  * Lock down the existing public surface so callers/mocks keep working.
  * Verify each wrapper produces the right family-specific cmd / env / stdin.
  * Confirm OpenCode goes through the shared runner with backend env injected.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pytest

from codepilot.ai_support import planner_execution


# ----- subprocess fakes ------------------------------------------------------


@dataclass
class _FakePopenResult:
    cmd: list[str]
    env: dict[str, str] | None
    stdin: io.BytesIO | None
    captured_stdin: bytes = b""


class _FakeProcess:
    def __init__(self, *, stdout_text: str, returncode: int = 0):
        self._stdout_text = stdout_text
        self._stdout_bytes = stdout_text.encode("utf-8")
        self.returncode = returncode
        self.pid = 12345
        self._poll_calls = 0
        self._stdin_buf = io.BytesIO()
        self.stdout = io.BytesIO(self._stdout_bytes)
        self.stderr = io.BytesIO(b"")
        self.stdin = self._stdin_buf

    def poll(self):
        self._poll_calls += 1
        # Return None once so the wait loop iterates, then exit.
        if self._poll_calls < 2:
            return None
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class _FakeSubprocess:
    DEVNULL = -3
    PIPE = -1

    class TimeoutExpired(Exception):
        def __init__(self, cmd, timeout):
            super().__init__(f"timeout {timeout} for {cmd!r}")
            self.cmd = cmd
            self.timeout = timeout

    def __init__(self, *, stdout_text: str = '{"ok": true}', returncode: int = 0):
        self.stdout_text = stdout_text
        self.returncode = returncode
        self.last: _FakePopenResult | None = None

    def Popen(self, cmd, **kwargs):
        proc = _FakeProcess(stdout_text=self.stdout_text, returncode=self.returncode)
        self.last = _FakePopenResult(
            cmd=list(cmd),
            env=kwargs.get("env"),
            stdin=proc.stdin,
        )
        return proc


# ----- shared callbacks ------------------------------------------------------


def _no_progress():
    return None


def _decode_chunk(chunk):
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="ignore")
    return str(chunk)


def _planner_kwargs():
    return {}


def _kill_tree(_pid):
    return None


def _terminate(_proc):
    return None


def _hint(_msg):
    return ""


def _get_progress():
    return None


# ----- claude / codex regression --------------------------------------------


class _FakeProvider:
    def __init__(self, name="Stub Provider", exe="/usr/bin/stub"):
        self.name = name
        self._exe = exe

    def find_executable(self):
        return self._exe


def test_claude_schema_prompt_invokes_subprocess_with_json_schema_flag():
    fake_subproc = _FakeSubprocess(stdout_text='{"summary": "claude-ok"}')

    result = planner_execution.run_claude_schema_prompt(
        "build something",
        {"type": "object"},
        planner="claude",
        timeout=5,
        normalize_agent_name=lambda x: x,
        resolve_cli_provider=lambda key, ref: _FakeProvider(name="Claude Code", exe="/bin/claude"),
        get_node_modules_path=lambda: "",
        subprocess_module=fake_subproc,
        planner_process_group_kwargs_fn=_planner_kwargs,
        decode_planner_chunk=_decode_chunk,
        kill_process_tree_fn=_kill_tree,
        terminate_planner_process_fn=_terminate,
        extract_error_hint=_hint,
        get_progress_callback=_get_progress,
    )

    assert isinstance(result, dict)
    assert fake_subproc.last is not None
    cmd = " ".join(fake_subproc.last.cmd)
    assert "--json-schema" in cmd, "claude wrapper must keep its --json-schema contract"
    assert "/bin/claude" in fake_subproc.last.cmd[0]


def test_codex_schema_prompt_signature_unchanged():
    """Regression: callers (task_planning, inspect, gateway) bind these kwargs by name."""
    import inspect as _inspect

    sig = _inspect.signature(planner_execution.run_codex_schema_prompt)
    expected = {
        "prompt", "schema",
        "project_path", "config_ref", "timeout",
        "check_provider_availability", "resolve_cli_provider",
        "subprocess_module", "planner_process_group_kwargs_fn",
        "decode_planner_chunk", "kill_process_tree_fn",
        "terminate_planner_process_fn", "extract_error_hint",
        "get_progress_callback",
    }
    assert expected.issubset(sig.parameters.keys())


# ----- opencode runner -------------------------------------------------------


def test_run_opencode_schema_prompt_is_exposed_from_planner_execution():
    """OpenCode goes through the shared runner; it must be importable here."""
    assert hasattr(planner_execution, "run_opencode_schema_prompt")
    assert callable(planner_execution.run_opencode_schema_prompt)


def test_opencode_schema_prompt_injects_backend_env_and_uses_run_subcommand():
    """End-to-end smoke test for the OpenCode wrapper."""
    from codepilot.core.config import AgentsConfig

    cfg = AgentsConfig.from_dict(
        {"providers": {"deepseek": {"api_key": "sk-ds", "base_url": "https://api.deepseek.com"}}}
    )
    fake_subproc = _FakeSubprocess(stdout_text='{"summary": "opencode-ok"}')

    result = planner_execution.run_opencode_schema_prompt(
        "对接外部接口",
        {"type": "object", "properties": {"summary": {"type": "string"}}},
        timeout=5,
        cfg_loader=lambda ref: cfg,
        resolve_cli_provider=lambda key, ref: _FakeProvider(name="OpenCode", exe="/bin/opencode"),
        subprocess_module=fake_subproc,
        planner_process_group_kwargs_fn=_planner_kwargs,
        decode_planner_chunk=_decode_chunk,
        kill_process_tree_fn=_kill_tree,
        terminate_planner_process_fn=_terminate,
        extract_error_hint=_hint,
        get_progress_callback=_get_progress,
    )

    assert isinstance(result, dict)
    assert fake_subproc.last is not None
    # Headless mode uses `run` subcommand.
    assert fake_subproc.last.cmd[1] == "run"
    # DeepSeek selection must inject OPENAI_API_KEY (+ OPENAI_BASE_URL) into env.
    assert fake_subproc.last.env is not None
    assert fake_subproc.last.env["OPENAI_API_KEY"] == "sk-ds"
    assert fake_subproc.last.env["OPENAI_BASE_URL"] == "https://api.deepseek.com"
    # User prompt must reach the wrapped argument.
    wrapped_prompt = fake_subproc.last.cmd[2]
    assert "对接外部接口" in wrapped_prompt
    assert "JSON" in wrapped_prompt or "json" in wrapped_prompt


def test_opencode_schema_prompt_raises_when_no_backend_key():
    """The friendly OpenCodeBackendUnavailable must surface when no key configured."""
    from codepilot.ai_support.opencode_runtime import OpenCodeBackendUnavailable
    from codepilot.core.config import AgentsConfig

    cfg = AgentsConfig.from_dict({})  # no providers
    fake_subproc = _FakeSubprocess()

    with pytest.raises(OpenCodeBackendUnavailable):
        planner_execution.run_opencode_schema_prompt(
            "anything",
            {"type": "object"},
            timeout=5,
            cfg_loader=lambda ref: cfg,
            resolve_cli_provider=lambda key, ref: _FakeProvider(name="OpenCode", exe="/bin/opencode"),
            subprocess_module=fake_subproc,
            planner_process_group_kwargs_fn=_planner_kwargs,
            decode_planner_chunk=_decode_chunk,
            kill_process_tree_fn=_kill_tree,
            terminate_planner_process_fn=_terminate,
            extract_error_hint=_hint,
            get_progress_callback=_get_progress,
        )


# ----- service-layer wrapper --------------------------------------------------


def test_service_exposes_run_opencode_schema_prompt_wrapper():
    from codepilot.ai_support import service

    assert hasattr(service, "_run_opencode_schema_prompt")
    assert callable(service._run_opencode_schema_prompt)
