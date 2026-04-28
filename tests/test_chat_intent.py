"""Integration tests for chat intent routing (mock AI, real DB + CLI)."""

from __future__ import annotations

import json
from types import SimpleNamespace

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
    result = runner.invoke(main, ["chat", "--no-ui"], input="# 帮我修复登录 bug\n/exit\n")

    assert result.exit_code == 0
    assert planning_called["count"] >= 1, "Planning should have been triggered"
    assert "阶段 1/3：正在识别输入意图" in result.output
    assert "阶段 2/3：正在评估需求完整度" in result.output
    assert "阶段 3/3：正在生成计划并执行任务" in result.output


def test_chat_auto_requirement_like_text_requires_explicit_prefix(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    planning_called = {"count": 0}
    monkeypatch.setattr(auto_mod, "generate_task_breakdown", lambda **kwargs: planning_called.update({"count": 1}) or {})

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="优化飞书任务面板\n/exit\n")

    assert result.exit_code == 0
    assert "不会直接执行" in result.output
    assert "需求 <内容>" in result.output
    assert planning_called["count"] == 0


def test_chat_command_heuristic_executes_local_status_command(tmp_path, monkeypatch):
    """A command-like sentence should execute the local workflow action directly."""
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
    assert "任务执行服务" in result.output
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == 0


def test_chat_classifier_failure_falls_back_to_question_instead_of_creating_task(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    monkeypatch.setattr(auto_mod, "classify_entry_intent", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        auto_mod,
        "answer_question_via_api",
        lambda **kwargs: "这是问答兜底回答。",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="我想先了解一下任务流\n/exit\n")

    assert result.exit_code == 0
    assert "这是问答兜底回答" in result.output
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


def test_classify_intent_heuristic_no_longer_forces_requirement_on_mid_sentence_verbs():
    from codepilot.ai_support.classifier import _heuristic_intent

    assert _heuristic_intent("我想先了解一下怎么优化任务流") is None


def test_classify_intent_heuristic_treats_information_queries_as_question():
    from codepilot.ai_support.classifier import _heuristic_intent

    assert _heuristic_intent("当前有多少任务，完成了多少") == "question"


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


def test_answer_question_for_tool_commands_uses_local_manifest_answer():
    from codepilot.ai_support.service import answer_question_via_api

    answer = answer_question_via_api(provider_key="", question="当前工具有哪些命令")

    assert "当前工具常用命令有这些" in answer
    assert "ai manifest" in answer
    assert "status" in answer


def test_broad_tool_question_does_not_force_local_manifest_answer(monkeypatch):
    from codepilot.ai_support.service import answer_question_via_api
    from codepilot.gateway import service as gateway_service

    monkeypatch.setattr(
        gateway_service,
        "call_text_prompt",
        lambda **kwargs: SimpleNamespace(ok=True, text="这是模型回答", source="api:test"),
    )

    answer = answer_question_via_api(
        provider_key="openai-gpt4o",
        question="这个工具有哪些功能和限制",
        base_url="http://localhost:11434/v1",
    )

    assert answer == "这是模型回答"


def test_question_runtime_data_uses_selected_project_and_feeds_stats_to_model(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    project_path = tmp_path / "demo"
    db.create_task(
        project="demo",
        title="已完成任务一",
        content="done",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    done_task = db.create_task(
        project="demo",
        title="已完成任务二",
        content="done",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.update_task(done_task["id"], status="done")

    captured = {}
    from codepilot.ai_support.service import answer_question_via_api
    from codepilot.gateway import service as gateway_service

    monkeypatch.setattr(
        gateway_service,
        "call_structured_prompt",
        lambda **kwargs: SimpleNamespace(
            ok=True,
            payload={
                "use_local_data": True,
                "project_scope": "current",
                "lookups": [{"tool": "task_stats"}],
                "answer_focus": "统计任务总数和完成数",
            },
            source="api:test",
        ),
    )

    def _fake_text_prompt(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return SimpleNamespace(ok=True, text="共有 2 个任务，已完成 1 个。", source="api:test")

    monkeypatch.setattr(gateway_service, "call_text_prompt", _fake_text_prompt)

    answer = answer_question_via_api(
        provider_key="openai-gpt4o",
        question="当前有多少个任务，完成了多少",
        project_path=str(project_path),
        base_url="http://localhost:11434/v1",
    )

    assert answer == "共有 2 个任务，已完成 1 个。"
    assert "已注册并已选中的项目" in captured["prompt"]
    assert "name=demo" in captured["prompt"]
    assert '"done": 1' in captured["prompt"]
    assert '"total": 2' in captured["prompt"]
    assert "确认终端当前目录" not in captured["prompt"]


def test_answer_question_without_api_key_uses_local_project_status_answer(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    project_path = tmp_path / "demo"
    running = db.create_task(
        project="demo",
        title="正在执行的任务",
        content="验证本地项目状态回答",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.update_task(running["id"], status="in_progress")

    from codepilot.ai_support.service import answer_question_via_api
    monkeypatch.setattr("codepilot.ai_support.classifier._has_local_question_answer_agent", lambda: False)

    answer = answer_question_via_api(
        provider_key="",
        question="当前项目状态怎么样",
        project_path=str(project_path),
    )

    assert "项目 `demo`" in answer
    assert "in_progress=1" in answer
    assert "未完成 1 个" in answer


def test_answer_question_without_api_key_uses_local_task_totals_answer(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    project_path = tmp_path / "demo"
    db.create_task(
        project="demo",
        title="任务一",
        content="统计任务数",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    done_task = db.create_task(
        project="demo",
        title="任务二",
        content="统计任务数",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.update_task(done_task["id"], status="done")

    from codepilot.ai_support.service import answer_question_via_api
    monkeypatch.setattr("codepilot.ai_support.classifier._has_local_question_answer_agent", lambda: False)

    answer = answer_question_via_api(
        provider_key="",
        question="现在任务一共多少，做完几个了",
        project_path=str(project_path),
    )

    assert "项目 `demo` 共有 2 个任务" in answer
    assert "已完成 1 个" in answer
    assert "未完成 1 个" in answer


def test_answer_question_without_api_key_uses_generic_runtime_fallback_for_running_tasks(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    project_path = tmp_path / "demo"
    running = db.create_task(
        project="demo",
        title="后台执行任务",
        content="看执行中任务",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )
    db.update_task(running["id"], status="in_progress")

    from codepilot.ai_support.service import answer_question_via_api
    monkeypatch.setattr("codepilot.ai_support.classifier._has_local_question_answer_agent", lambda: False)

    answer = answer_question_via_api(
        provider_key="",
        question="现在有哪些任务在跑",
        project_path=str(project_path),
    )

    assert "当前执行中的任务有" in answer
    assert "#1 后台执行任务" in answer


def test_answer_question_without_api_key_uses_local_projects_answer(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    db.register_project("other", str(other))

    from codepilot.ai_support.service import answer_question_via_api
    monkeypatch.setattr("codepilot.ai_support.classifier._has_local_question_answer_agent", lambda: False)

    answer = answer_question_via_api(provider_key="", question="当前有哪些项目")

    assert "当前已注册项目" in answer
    assert "`demo`" in answer
    assert "`other`" in answer


def test_answer_question_without_api_key_prefers_local_cli_agent_when_available(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    project_path = tmp_path / "demo"
    db.create_task(
        project="demo",
        title="任务一",
        content="统计任务数",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    from codepilot.ai_support.service import answer_question_via_api
    from codepilot.gateway import service as gateway_service

    monkeypatch.setattr("codepilot.ai_support.classifier._has_local_question_answer_agent", lambda: True)
    monkeypatch.setattr(
        gateway_service,
        "call_text_prompt",
        lambda **kwargs: SimpleNamespace(ok=True, text="CLI 智能体汇报：当前 1 个任务。", source="cli:codex"),
    )

    answer = answer_question_via_api(
        provider_key="",
        question="现在任务有多少",
        project_path=str(project_path),
    )

    assert answer == "CLI 智能体汇报：当前 1 个任务。"


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


def test_chat_natural_language_task_status_executes_local_command(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    called = {}

    monkeypatch.setattr(
        auto_mod,
        "render_project_dashboard",
        lambda *args, **kwargs: called.update({"args": args, "kwargs": kwargs}),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="看看 demo 项目的任务状态\n/exit\n")

    assert result.exit_code == 0
    assert called["args"][0] == "demo"
    assert "任务面板" in called["kwargs"]["title"]


def test_chat_natural_language_delete_uses_numbered_choice(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    project_path = tmp_path / "demo"
    first = db.create_task(
        project="demo",
        title="自然语言删除任务一",
        content="验证候选选择",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.create_task(
        project="demo",
        title="自然语言删除任务二",
        content="验证候选选择",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="删除任务\n1\n/exit\n")

    assert result.exit_code == 0
    assert "回复数字继续" in result.output
    assert db.get_task(first["id"]) is None


def test_chat_tool_command_question_uses_local_manifest_answer(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(main, ["chat", "--no-ui"], input="当前工具有哪些命令\n/exit\n")

    assert result.exit_code == 0
    assert "当前工具常用命令有这些" in result.output
    assert "ai manifest" in result.output



