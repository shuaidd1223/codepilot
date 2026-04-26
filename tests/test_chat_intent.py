"""Integration tests for chat intent routing (mock AI, real DB + CLI)."""

from __future__ import annotations

import json

import click
from click.testing import CliRunner

from codepilot import __version__
from codepilot.storage import database as db
from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from codepilot.commands import auto_chat as auto_chat_mod
from tests.chat_flow_testkit import register_project


# ─── heuristic routing in chat ───────────────────────────────────────────────

def test_chat_question_heuristic_does_not_create_task(tmp_path, monkeypatch):
    """A question like '怎么用' should not create any task."""
    register_project(tmp_path, monkeypatch)

    # Mock answer_question_via_api to return a canned answer
    monkeypatch.setattr(
        auto_mod,
        "answer_question_via_api",
        lambda **kwargs: "CodePilot 是一个工作流工具。",
    )
    # Stub out classify_intent to use heuristic only (no AI call)
    original_classify = auto_mod.classify_intent

    def _heuristic_only(text, **kwargs):
        from codepilot.ai_support.service import _heuristic_intent
        intent = _heuristic_intent(text)
        if intent:
            return {"intent": intent, "reason": "heuristic", "source": "heuristic"}
        return {"intent": "requirement", "reason": "default", "source": "default"}

    monkeypatch.setattr(auto_mod, "classify_intent", _heuristic_only)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="怎么用这个工具\n/exit\n")

    assert result.exit_code == 0
    assert "CodePilot 是一个工作流工具" in result.output
    assert "阶段 1/3：正在识别输入意图" in result.output
    assert "阶段 2/2：正在检索上下文并回答" in result.output
    # Should NOT have created any task
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 0


def test_chat_requirement_heuristic_triggers_planning(tmp_path, monkeypatch):
    """A requirement like '帮我修复 bug' should trigger the planning flow."""
    register_project(tmp_path, monkeypatch)

    planning_called = {"count": 0}

    def _mock_breakdown(**kwargs):
        planning_called["count"] += 1
        return {
            "summary": "修复 bug",
            "complexity": "simple",
            "should_split": False,
            "tasks": [{"title": "修复 bug", "priority": "P2"}],
        }

    monkeypatch.setattr(auto_mod, "generate_task_breakdown", _mock_breakdown)
    monkeypatch.setattr(auto_mod, "run_backlog", lambda *a, **kw: {
        "processed": 0, "done": 0, "failed": 0, "requeued": 0, "cancelled": 0,
    })

    def _heuristic_only(text, **kwargs):
        from codepilot.ai_support.service import _heuristic_intent
        intent = _heuristic_intent(text)
        if intent:
            return {"intent": intent, "reason": "heuristic", "source": "heuristic"}
        return {"intent": "requirement", "reason": "default", "source": "default"}

    monkeypatch.setattr(auto_mod, "classify_intent", _heuristic_only)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="帮我修复登录 bug\n/exit\n")

    assert result.exit_code == 0
    assert planning_called["count"] >= 1, "Planning should have been triggered"
    assert "阶段 1/3：正在识别输入意图" in result.output
    assert "阶段 2/3：正在评估需求完整度" in result.output
    assert "阶段 3/3：正在生成计划并执行任务" in result.output


def test_chat_command_heuristic_shows_help(tmp_path, monkeypatch):
    """A command like '查看状态' should show CLI help, not create task."""
    register_project(tmp_path, monkeypatch)

    def _heuristic_only(text, **kwargs):
        from codepilot.ai_support.service import _heuristic_intent
        intent = _heuristic_intent(text)
        if intent:
            return {"intent": intent, "reason": "heuristic", "source": "heuristic"}
        return {"intent": "requirement", "reason": "default", "source": "default"}

    monkeypatch.setattr(auto_mod, "classify_intent", _heuristic_only)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="查看状态\n/exit\n")

    assert result.exit_code == 0
    assert "codepilot status" in result.output
    # No task created
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 0


def test_chat_slash_version_shows_current_version_without_creating_task(tmp_path, monkeypatch):
    """The interactive chat command dispatcher should handle /version locally."""
    register_project(tmp_path, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="/version\n/exit\n")

    assert result.exit_code == 0
    assert f"CodePilot {__version__}" in result.output
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 0


# ─── classify_intent fallback chain ──────────────────────────────────────────

def test_classify_intent_uses_heuristic_first(monkeypatch):
    """Heuristic match should short-circuit without calling any AI."""
    from codepilot.ai_support.service import classify_intent

    result = classify_intent("怎么安装这个工具")
    assert result["intent"] == "question"
    assert result["source"] == "heuristic"


def test_classify_intent_defaults_to_requirement_on_failure(monkeypatch):
    """When heuristic misses and all AI fails, default to requirement."""
    from codepilot.ai_support import classifier as classifier_mod
    from codepilot.gateway import service as ai_gateway
    from codepilot.ai_support.service import classify_intent

    # Heuristic must miss so we exercise the AI path.
    monkeypatch.setattr(classifier_mod, "_heuristic_intent", lambda t: None)

    # Force the unified gateway to report total failure.
    def _gateway_fail(request):
        return ai_gateway.GatewayResponse(
            ok=False,
            source="cli:none",
            error="backend disabled for this test",
        )

    monkeypatch.setattr(classifier_mod, "call_structured", _gateway_fail, raising=False)
    # The module imports call_structured lazily, so patch in ai_gateway too.
    monkeypatch.setattr(ai_gateway, "call_structured", _gateway_fail)

    result = classify_intent("some ambiguous input")
    assert result["intent"] == "requirement"
    assert result["source"] == "default"


def test_classify_intent_falls_back_to_cli_when_api_key_missing(monkeypatch, tmp_path):
    """Configured API without a key should still route through CLI fallback."""
    from codepilot.ai_support import classifier as classifier_mod
    from codepilot.ai_support import providers as ai_providers
    from codepilot.ai_support.service import classify_intent

    monkeypatch.setattr(classifier_mod, "_heuristic_intent", lambda t: None)
    monkeypatch.delenv("CODEPILOT_TEST_AI_GATEWAY_KEY", raising=False)
    monkeypatch.setitem(
        ai_providers.API_PROVIDERS,
        "missing-key-test",
        ai_providers.APIProvider(
            name="Missing Key Test",
            provider_type="openai",
            model="test-model",
            api_env_vars=("CODEPILOT_TEST_AI_GATEWAY_KEY",),
        ),
    )

    api_calls = []
    cli_calls = []

    def _api_should_be_skipped(provider, prompt):
        api_calls.append({"provider": provider, "prompt": prompt})
        return json.dumps({"intent": "task", "reason": "unexpected api"})

    def _fake_claude_schema_prompt(prompt, schema, **kw):
        cli_calls.append({"prompt": prompt, "schema": schema, "kwargs": kw})
        return {"intent": "question", "reason": "from CLI fallback"}

    monkeypatch.setattr(ai_providers, "_run_api_provider", _api_should_be_skipped)
    monkeypatch.setattr("codepilot.ai_support.service._run_claude_schema_prompt", _fake_claude_schema_prompt)

    result = classify_intent(
        "需要判断这段输入的类型",
        project_path=str(tmp_path),
        classifier_provider="missing-key-test",
        timeout=7,
    )

    assert result == {
        "intent": "question",
        "reason": "from CLI fallback",
        "source": "cli:claude",
    }
    assert api_calls == []
    assert len(cli_calls) == 1
    assert cli_calls[0]["schema"] == classifier_mod.INTENT_SCHEMA
    assert cli_calls[0]["kwargs"]["project_path"] == str(tmp_path)
    assert cli_calls[0]["kwargs"]["timeout"] == 7


def test_classify_intent_command_guardrail_avoids_non_cli_false_positive(monkeypatch):
    """AI returning 'command' for unrelated text should be downgraded."""
    from codepilot.ai_support import classifier as classifier_mod
    from codepilot.gateway import service as ai_gateway
    from codepilot.ai_support.service import classify_intent

    monkeypatch.setattr(classifier_mod, "_heuristic_intent", lambda t: None)

    def _gateway_false_command(_request):
        return ai_gateway.GatewayResponse(
            ok=True,
            source="api:test",
            payload={"intent": "command", "reason": "误判"},
        )

    monkeypatch.setattr(classifier_mod, "call_structured", _gateway_false_command, raising=False)
    monkeypatch.setattr(ai_gateway, "call_structured", _gateway_false_command)

    result = classify_intent("11")
    assert result["intent"] == "requirement"
    assert result["source"] == "guardrail"


def test_chat_ui_starts_via_detached_webui_service(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    seen = {"called": False, "new_process_group": False}

    class _Proc:
        pid = 4242

    monkeypatch.setattr(auto_chat_mod, "_is_chat_ui_healthy", lambda port, host="127.0.0.1", timeout=0.25: False)
    monkeypatch.setattr(
        auto_chat_mod,
        "no_window_kwargs",
        lambda *, new_process_group=False: seen.update(
            {"called": True, "new_process_group": bool(new_process_group)}
        )
        or {"creationflags": 123},
    )

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Proc()

    monkeypatch.setattr(auto_chat_mod.subprocess, "Popen", fake_popen)

    handle = auto_chat_mod._start_chat_ui(9912)

    assert handle and handle["managed"] is True
    assert handle["launcher_pid"] == 4242
    assert calls
    cmd, kwargs = calls[0]
    assert cmd[:5] == [auto_chat_mod.sys.executable, "-m", "codepilot", "ui", "start"]
    assert "--no-daemon" in cmd
    assert cmd[-2:] == ["--port", "9912"]
    assert kwargs["close_fds"] is True
    assert kwargs["creationflags"] == 123
    assert seen["called"] is True
    assert seen["new_process_group"] is True


def test_chat_ctrl_c_abort_exits_cleanly(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    def _raise_abort(*args, **kwargs):
        raise click.Abort()

    monkeypatch.setattr(auto_chat_mod.click, "prompt", _raise_abort)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"])

    assert result.exit_code == 0
    assert "会话已结束" in result.output
    assert "Aborted!" not in result.output


def test_chat_exit_does_not_stop_global_webui_service(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_chat_mod, "_start_chat_ui", lambda port=8766: {"managed": True, "port": port})

    calls = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(auto_chat_mod.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["chat"], input="/exit\n")

    assert result.exit_code == 0
    assert calls == []



