"""Tests for gateway resolution / execute being registry-driven (task #213).

After this slice:
  * ``_CLAUDE_PLANNERS`` literal is gone — claude/opus/sonnet/haiku planners
    resolve through the CLI family registry.
  * ``resolve_text_cli_candidates`` reads its order from
    ``cfg.automation.fallback_cli_order`` (defaults to ``["claude","codex","opencode"]``).
  * ``execute_structured_cli_call`` dispatches to the correct schema runner
    via the registry instead of hard-coded if/else.
  * Opencode is a first-class member of both code paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from codepilot.gateway import resolution as resolution_mod
from codepilot.gateway.resolution import (
    resolve_structured_cli_call,
    resolve_text_cli_candidates,
)
from codepilot.gateway.types import GatewayRequest


@dataclass
class FakeCLIProvider:
    exe: str = ""

    def find_executable(self):
        return self.exe


# ----- _CLAUDE_PLANNERS removal ----------------------------------------------


def test_claude_planners_literal_no_longer_present():
    """Regression guard: the family decision must come from the registry, not a hard-coded set."""
    # The old constant must be gone — its presence would mean the if/else
    # ladder still exists somewhere in resolution.py.
    assert not hasattr(resolution_mod, "_CLAUDE_PLANNERS")


# ----- structured CLI dispatch via registry ----------------------------------


def test_resolve_structured_cli_call_resolves_opencode_planner():
    resolved = resolve_structured_cli_call(
        GatewayRequest(prompt="hi", planner="opencode", schema={"type": "object"})
    )
    assert resolved.cli_name == "opencode"
    assert resolved.source == "cli:opencode"


def test_resolve_structured_cli_call_resolves_oc_alias_to_opencode():
    resolved = resolve_structured_cli_call(
        GatewayRequest(prompt="hi", planner="oc", schema={"type": "object"})
    )
    assert resolved.cli_name == "opencode"


def test_resolve_structured_cli_call_still_handles_claude_variants():
    """Regression: claude-opus / claude-sonnet / claude-haiku still route to claude."""
    for variant in ("claude-opus", "claude-sonnet", "claude-haiku"):
        resolved = resolve_structured_cli_call(
            GatewayRequest(prompt="hi", planner=variant, schema={"type": "object"})
        )
        assert resolved.cli_name == "claude", f"{variant} should route to claude"


def test_resolve_structured_cli_call_falls_back_to_codex_for_unknown_planner():
    resolved = resolve_structured_cli_call(
        GatewayRequest(prompt="hi", planner="completely-unknown", schema={"type": "object"})
    )
    assert resolved.cli_name == "codex"


# ----- text CLI candidate order ---------------------------------------------


def _fake_all_present(monkeypatch):
    """Make every CLI family appear installed."""

    def _fake(cli_name, _provider_ref):
        return FakeCLIProvider(exe=f"/bin/{cli_name}")

    monkeypatch.setattr("codepilot.ai_support.providers.resolve_cli_provider", _fake)


def test_default_fallback_order_includes_opencode_last(monkeypatch):
    _fake_all_present(monkeypatch)

    candidates, _ = resolve_text_cli_candidates(GatewayRequest(prompt="hi"))

    # Default order: claude, codex, opencode
    assert [c.cli_name for c in candidates] == ["claude", "codex", "opencode"]


def test_custom_fallback_order_from_config_is_respected(tmp_path: Path, monkeypatch):
    _fake_all_present(monkeypatch)
    (tmp_path / "AGENTS.toml").write_text(
        '[automation]\nfallback_cli_order = ["opencode", "codex"]\n',
        encoding="utf-8",
    )

    candidates, _ = resolve_text_cli_candidates(
        GatewayRequest(prompt="hi", project_path=str(tmp_path))
    )

    assert [c.cli_name for c in candidates] == ["opencode", "codex"]


def test_text_cli_candidate_for_opencode_uses_run_subcommand(monkeypatch):
    _fake_all_present(monkeypatch)

    candidates, _ = resolve_text_cli_candidates(GatewayRequest(prompt="hi", planner="opencode"))

    opencode_candidate = next(c for c in candidates if c.cli_name == "opencode")
    # `opencode run` is the headless invocation; the CLI itself accepts the
    # prompt via stdin (we feed it via subprocess.run input=...).
    assert opencode_candidate.cmd[1] == "run"


def test_no_duplicate_claude_in_default_order(monkeypatch):
    """Regression for the L142-143 dead code: claude must appear at most once."""
    _fake_all_present(monkeypatch)

    candidates, _ = resolve_text_cli_candidates(
        GatewayRequest(prompt="hi", planner="claude")
    )

    cli_names = [c.cli_name for c in candidates]
    assert cli_names.count("claude") == 1


# ----- execute_structured_cli_call dispatch ---------------------------------


def test_execute_structured_cli_call_dispatches_opencode():
    """Asserts the gateway execute layer can route a resolved opencode call."""
    from codepilot.gateway.execute import execute_structured_cli_call
    from codepilot.gateway.resolution import ResolvedStructuredCLICall

    captured = {}

    def fake_run_opencode(prompt, schema, **kwargs):
        captured["called"] = "opencode"
        captured["prompt"] = prompt
        captured["schema"] = schema
        return {"summary": "ok"}

    import codepilot.ai_support.service as service_mod

    original = service_mod._run_opencode_schema_prompt
    service_mod._run_opencode_schema_prompt = fake_run_opencode
    try:
        result = execute_structured_cli_call(
            GatewayRequest(prompt="对接外部接口", planner="opencode", schema={"type": "object"}),
            ResolvedStructuredCLICall(cli_name="opencode", planner="opencode", source="cli:opencode"),
        )
    finally:
        service_mod._run_opencode_schema_prompt = original

    assert captured["called"] == "opencode"
    assert captured["prompt"] == "对接外部接口"
    assert result == {"summary": "ok"}


def test_execute_structured_cli_call_unknown_family_raises():
    from codepilot.gateway.execute import execute_structured_cli_call
    from codepilot.gateway.resolution import ResolvedStructuredCLICall

    with pytest.raises((ValueError, RuntimeError, KeyError)):
        execute_structured_cli_call(
            GatewayRequest(prompt="x", schema={"type": "object"}),
            ResolvedStructuredCLICall(cli_name="ghost-family", planner="ghost", source="cli:ghost"),
        )
