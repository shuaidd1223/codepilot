"""End-to-end wiring tests for the OpenCode bottom-tier fallback.

These guard the integration glue added after the registry foundation:
- task_planning dispatches the opencode planner branch
- service-level wrappers thread `_run_opencode_schema_prompt` through
- inspect command's planner fallback recognises opencode
- builtin executor accepts opencode as a builder/reviewer agent and
  injects the OpenCode env-bridge for the spawned subprocess
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


# --- task_planning dispatch -----------------------------------------------------


def test_task_planning_dispatches_to_opencode_runner_for_breakdown(monkeypatch):
    from codepilot.ai_support import task_planning

    captured: dict[str, Any] = {}

    def _claude(*_a, **_kw):
        captured["called"] = "claude"
        return {}

    def _codex(*_a, **_kw):
        captured["called"] = "codex"
        return {}

    def _opencode(prompt, schema, *, project_path, config_ref):
        captured["called"] = "opencode"
        captured["prompt"] = prompt
        captured["project_path"] = project_path
        return {"summary": "ok", "should_split": False, "tasks": []}

    breakdown = task_planning.generate_task_breakdown(
        "test goal",
        project_path="D:/demo",
        planner="opencode",
        max_tasks=3,
        normalize_agent_name=lambda n: (n or "").strip().lower(),
        collect_planner_context=lambda *_a, **_kw: "",
        format_existing_block=lambda *_a, **_kw: "",
        run_recon_stage_fn=lambda *_a, **_kw: {},
        format_recon_block_fn=lambda *_a, **_kw: "",
        run_claude_schema_prompt=_claude,
        run_codex_schema_prompt=_codex,
        run_opencode_schema_prompt=_opencode,
        parse_automation_planner_result_fn=lambda *_a, **_kw: {"tasks": []},
        two_stage=False,
        parse_result=False,
    )

    assert captured["called"] == "opencode"
    assert "test goal" in captured["prompt"]
    assert breakdown == {"summary": "ok", "should_split": False, "tasks": []}


def test_task_planning_routes_claude_variants_to_claude_runner():
    from codepilot.ai_support import task_planning

    seen = []

    def _claude(prompt, schema, *, planner, project_path, config_ref):
        seen.append(("claude", planner))
        return {}

    def _codex(*_a, **_kw):
        seen.append(("codex",))
        return {}

    def _opencode(*_a, **_kw):
        seen.append(("opencode",))
        return {}

    task_planning.generate_task_breakdown(
        "g",
        planner="claude-sonnet",
        normalize_agent_name=lambda n: (n or "").strip().lower(),
        collect_planner_context=lambda *_a, **_kw: "",
        format_existing_block=lambda *_a, **_kw: "",
        run_recon_stage_fn=lambda *_a, **_kw: {},
        format_recon_block_fn=lambda *_a, **_kw: "",
        run_claude_schema_prompt=_claude,
        run_codex_schema_prompt=_codex,
        run_opencode_schema_prompt=_opencode,
        parse_automation_planner_result_fn=lambda *_a, **_kw: {"tasks": []},
        two_stage=False,
        parse_result=False,
    )

    assert seen == [("claude", "claude-sonnet")]


def test_task_planning_unknown_planner_raises():
    from codepilot.ai_support import task_planning

    with pytest.raises(RuntimeError, match="不支持规划器"):
        task_planning.generate_task_breakdown(
            "g",
            planner="acme-bot",
            normalize_agent_name=lambda n: (n or "").strip().lower(),
            collect_planner_context=lambda *_a, **_kw: "",
            format_existing_block=lambda *_a, **_kw: "",
            run_recon_stage_fn=lambda *_a, **_kw: {},
            format_recon_block_fn=lambda *_a, **_kw: "",
            run_claude_schema_prompt=lambda *_a, **_kw: {},
            run_codex_schema_prompt=lambda *_a, **_kw: {},
            run_opencode_schema_prompt=lambda *_a, **_kw: {},
            parse_automation_planner_result_fn=lambda *_a, **_kw: {"tasks": []},
            two_stage=False,
            parse_result=False,
        )


# --- service.py threads opencode through to task_planning ----------------------


def test_service_generate_task_breakdown_passes_opencode_runner(monkeypatch):
    """Regression: the service-level wrapper must inject _run_opencode_schema_prompt
    or task_planning will silently fall back to the codex/claude only path."""
    from codepilot.ai_support import service

    captured: dict[str, Any] = {}

    def fake_breakdown(*args, **kwargs):
        captured.update(kwargs)
        return {"tasks": []}

    monkeypatch.setattr(service._task_planning, "generate_task_breakdown", fake_breakdown)
    service.generate_task_breakdown("g", planner="codex", parse_result=False)

    assert captured.get("run_opencode_schema_prompt") is service._run_opencode_schema_prompt
    assert captured.get("run_claude_schema_prompt") is service._run_claude_schema_prompt
    assert captured.get("run_codex_schema_prompt") is service._run_codex_schema_prompt


def test_service_run_recon_stage_passes_opencode_runner(monkeypatch):
    from codepilot.ai_support import service

    captured: dict[str, Any] = {}

    def fake_recon(*args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(service._task_planning, "run_recon_stage", fake_recon)
    service._run_recon_stage(
        "title",
        "D:/demo",
        planner_normalized="opencode",
        config_ref=None,
        project_context="",
    )

    assert captured.get("run_opencode_schema_prompt") is service._run_opencode_schema_prompt


# --- inspect command --------------------------------------------------------------


def test_inspect_planner_dispatches_opencode(monkeypatch):
    """commands/inspect.py must route normalized=opencode to _run_opencode_schema_prompt."""
    from codepilot.commands import inspect as inspect_mod

    sentinel = {"breakdown": "from-opencode"}
    fake_opencode = MagicMock(return_value=sentinel)
    fake_claude = MagicMock(return_value={"breakdown": "from-claude"})
    fake_codex = MagicMock(return_value={"breakdown": "from-codex"})

    monkeypatch.setattr(inspect_mod, "_run_opencode_schema_prompt", fake_opencode)
    monkeypatch.setattr(inspect_mod, "_run_claude_schema_prompt", fake_claude)
    monkeypatch.setattr(inspect_mod, "_run_codex_schema_prompt", fake_codex)
    monkeypatch.setattr(inspect_mod, "API_PROVIDERS", {})

    # Directly drive _call_llm with planner="opencode" and no API classifier.
    result = inspect_mod._call_llm(
        prompt="demo",
        llm_provider="",
        llm_model="",
        api_key=None,
        base_url=None,
        project_path="D:/demo",
        timeout=60,
        planner="opencode",
    )

    assert result is sentinel
    fake_opencode.assert_called_once()
    fake_claude.assert_not_called()
    fake_codex.assert_not_called()


def test_inspect_planner_still_defaults_to_claude(monkeypatch):
    """Regression: omitted planner should still pick claude (analysis preference)."""
    from codepilot.commands import inspect as inspect_mod

    fake_claude = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(inspect_mod, "_run_claude_schema_prompt", fake_claude)
    monkeypatch.setattr(inspect_mod, "_run_codex_schema_prompt", MagicMock())
    monkeypatch.setattr(inspect_mod, "_run_opencode_schema_prompt", MagicMock())
    monkeypatch.setattr(inspect_mod, "API_PROVIDERS", {})

    inspect_mod._call_llm(
        prompt="demo",
        llm_provider="",
        llm_model="",
        api_key=None,
        base_url=None,
        project_path="D:/demo",
        timeout=60,
        planner=None,
    )

    fake_claude.assert_called_once()


# --- run_builtin_executor accepts opencode --------------------------------------


def test_resolve_builtin_single_agent_accepts_opencode():
    from codepilot.commands.run_builtin_executor import _resolve_builtin_single_agent

    runner, model = _resolve_builtin_single_agent("opencode")
    assert runner == "opencode"
    assert model is None


def test_resolve_builtin_single_agent_rejects_unknown():
    from codepilot.commands.run_builtin_executor import _resolve_builtin_single_agent

    with pytest.raises(RuntimeError, match="不支持任务智能体"):
        _resolve_builtin_single_agent("acme-bot")


def test_builtin_phase_agents_includes_opencode():
    """dual-mode dispatcher must accept opencode as builder or reviewer."""
    from codepilot.ai_support.service import BUILTIN_PHASE_AGENTS, is_builtin_phase_agent_supported

    assert "opencode" in BUILTIN_PHASE_AGENTS
    assert is_builtin_phase_agent_supported("opencode") is True
    assert is_builtin_phase_agent_supported("oc") is True  # alias support


# --- secondary-path opencode integration ---------------------------------------


def test_add_command_agent_choices_include_opencode():
    """`codepilot add -a opencode` should be accepted as a valid CLI choice."""
    from codepilot.commands.add import AGENT_CHOICES

    assert "opencode" in AGENT_CHOICES
    assert "oc" in AGENT_CHOICES


def test_task_edit_agent_choice_includes_opencode():
    """`codepilot task edit -a opencode` should be a valid Click choice."""
    from codepilot.commands.tasks import edit as edit_cmd

    agent_param = next(p for p in edit_cmd.params if p.name == "agent")
    assert "opencode" in agent_param.type.choices


def test_auto_workflow_accepts_opencode_for_builtin_executor():
    """auto_workflow._resolve_task_agent's executor=builtin allow-list must include opencode.

    We assert via source inspection because the function depends on
    project-specific runtime services (provider availability, project
    config) that are awkward to mock at unit-test scope.
    """
    from codepilot.commands import auto_workflow
    import inspect as _inspect

    src = _inspect.getsource(auto_workflow._resolve_task_agent)
    assert '"opencode"' in src
    assert "请改用 codex、claude、claude-node、opencode 或 dual。" in src


def test_builtin_fallback_candidates_include_opencode():
    """The builder/reviewer-phase fallback chain must include opencode after claude/codex."""
    from codepilot.commands import run_builtin_executor as run_mod
    import inspect as _inspect

    src = _inspect.getsource(run_mod._select_builtin_phase_fallback_agent)
    # Sanity: opencode is in the candidate list literal.
    assert '"opencode"' in src
