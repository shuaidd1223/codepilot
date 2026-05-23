"""Unit tests for the shared chat/webui/go interaction controller skeleton."""

from __future__ import annotations

from codepilot.ai_support.interaction_controller import (
    parse_intent_prefix,
    resolve_turn_intent,
)


def _q(text: str, *, qid: str = "q1") -> dict:
    return {
        "id": qid,
        "type": "text",
        "text": text,
        "options": [],
        "allow_free_text": False,
    }


def test_parse_intent_prefix_supports_question_task_requirement():
    assert parse_intent_prefix("? 怎么用") == ("question", "怎么用")
    assert parse_intent_prefix("! 修复登录") == ("task", "修复登录")
    assert parse_intent_prefix("# 做一个规划") == ("requirement", "做一个规划")
    assert parse_intent_prefix("问题 现在什么状态") == ("question", "现在什么状态")
    assert parse_intent_prefix("任务 修一下文档") == ("task", "修一下文档")
    assert parse_intent_prefix("需求 优化任务面板") == ("requirement", "优化任务面板")
    assert parse_intent_prefix("普通输入") == (None, "普通输入")


def test_resolve_turn_intent_prefers_overrides_then_classifier_then_fallback():
    assert resolve_turn_intent("x", category="question") == "question"
    assert resolve_turn_intent("x", forced_intent="task") == "task"
    assert resolve_turn_intent("x", classify_fn=lambda *_a, **_kw: "command") == "command"
    assert resolve_turn_intent("x", classify_fn=lambda *_a, **_kw: "unknown") == "requirement"
    assert resolve_turn_intent("x", classify_fn=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom"))) == "requirement"


def test_classify_entry_intent_skips_classifier_by_default(monkeypatch):
    from codepilot.commands import auto as auto_cmd

    calls: list[str] = []

    def _classifier_should_not_run(text, **_kwargs):
        calls.append(text)
        return {"intent": "task", "source": "unexpected"}

    monkeypatch.setattr(auto_cmd, "classify_intent", _classifier_should_not_run)

    intent = resolve_turn_intent(
        "ambiguous input",
        category="auto",
        classify_fn=auto_cmd.classify_entry_intent,
        classify_kwargs={"project_info": {"name": "demo", "path": "D:/repo/demo"}},
        fallback_intent="requirement",
    )

    assert intent == "requirement"
    assert calls == []

