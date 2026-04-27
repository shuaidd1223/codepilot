"""Shared quality-gate helpers test.

Covers the new ``codepilot.commands.task_quality`` module and verifies the
keyword lists behave correctly for planner vs inspect contexts.
"""

from __future__ import annotations

from codepilot.commands import auto_workflow as aw
from codepilot.commands import inspect as inspect_cmd
from codepilot.commands.task_quality import (
    BASE_FILLER_KEYWORDS,
    INSPECT_FILLER_KEYWORDS,
    PlannerQualitySummary,
    PLANNER_FILLER_KEYWORDS,
    evidence_grounded_in,
    evaluate_planner_task,
    looks_generic,
    planner_quality_blocking_messages,
)


def test_base_keywords_are_shared_by_both_contexts():
    for keyword in BASE_FILLER_KEYWORDS:
        assert keyword in PLANNER_FILLER_KEYWORDS
        assert keyword in INSPECT_FILLER_KEYWORDS


def test_planner_list_keeps_bare_marker_words_out_of_inspect_keywords():
    """Inspect signals can legitimately cite code comment markers, so bare
    ``todo`` must NOT be a filler keyword there. The planner stage can
    afford to be stricter since user-supplied requirements rarely contain
    bare English placeholders."""
    assert "todo" in PLANNER_FILLER_KEYWORDS
    assert "placeholder" in PLANNER_FILLER_KEYWORDS
    assert "todo" not in INSPECT_FILLER_KEYWORDS
    assert "placeholder" not in INSPECT_FILLER_KEYWORDS


def test_looks_generic_is_case_insensitive():
    assert looks_generic("PLACEHOLDER TASK", PLANNER_FILLER_KEYWORDS) is True
    assert looks_generic("补一下文档 再顺手加点日志", BASE_FILLER_KEYWORDS) is True


def test_looks_generic_empty_text_is_false():
    assert looks_generic("", PLANNER_FILLER_KEYWORDS) is False
    assert looks_generic("   ", PLANNER_FILLER_KEYWORDS) is False


def test_looks_generic_unrelated_text_is_false():
    assert looks_generic("修复登录闪退", PLANNER_FILLER_KEYWORDS) is False


def test_inspect_filler_does_not_fire_on_legitimate_marker_reference():
    """A candidate title citing ``TODO`` should not be treated as filler
    when it points at a concrete code marker."""
    text = "修复 foo.py:42 的 TODO 标记引用"
    assert looks_generic(text, INSPECT_FILLER_KEYWORDS) is False


def test_planner_filler_fires_on_explicit_filler_titles():
    assert looks_generic("占位任务：稍后再补", PLANNER_FILLER_KEYWORDS) is True
    assert looks_generic("misc cleanup", PLANNER_FILLER_KEYWORDS) is True


def test_evidence_grounded_in_bidirectional_match():
    tokens = {"codepilot/foo.py:42 TODO handle timeout", "signal 3"}
    assert evidence_grounded_in("signal 3: foo.py:42", tokens) is True
    assert evidence_grounded_in("foo.py:42 TODO", tokens) is True


def test_evidence_grounded_in_rejects_when_no_token_overlap():
    tokens = {"codepilot/foo.py:42 TODO handle timeout"}
    assert evidence_grounded_in("上次线上讨论记录", tokens) is False


def test_evidence_grounded_in_ignores_trivial_short_tokens():
    """Default min_token_length=4 prevents accidental matches on 'api'/'go'."""
    assert evidence_grounded_in("某次讨论", {"api", "go"}) is False


def test_evidence_grounded_in_empty_inputs_are_false():
    assert evidence_grounded_in("", {"signal 3"}) is False
    assert evidence_grounded_in("signal 3", set()) is False


def test_evaluate_planner_task_reports_missing_fields_and_placeholder_state():
    advisory, summary = evaluate_planner_task(
        {
            "title": "placeholder task",
            "goal": "待补充",
            "acceptance_criteria": [],
            "files": [],
            "evidence": "",
        },
        index=2,
    )
    assert any("Task 2 looks placeholder-like" in msg for msg in advisory)
    assert any("Task 2 is missing `acceptance_criteria` entries." == msg for msg in advisory)
    assert summary.total_tasks == 1
    assert summary.placeholder_like_tasks == 1
    assert summary.ac_missing_count == 1
    assert summary.files_missing_count == 1
    assert summary.evidence_missing_count == 1


def test_planner_quality_blocking_messages_only_fire_when_all_tasks_fail_same_gate():
    blocking = planner_quality_blocking_messages(
        PlannerQualitySummary(
            total_tasks=2,
            placeholder_like_tasks=2,
            title_missing_count=1,
            goal_missing_count=0,
            ac_missing_count=2,
            files_missing_count=1,
            evidence_missing_count=2,
        )
    )
    assert "All tasks look placeholder-like and lack concrete deliverables." in blocking
    assert "All tasks are missing `acceptance_criteria` entries." in blocking
    assert any("fabricating tasks" in msg for msg in blocking)
    assert "All tasks are missing `title`." not in blocking
    assert "All tasks are missing `files` entries." not in blocking


# ─── planner quality gate uses the shared list ───────────────────────────────


def test_planner_quality_gate_flags_shared_filler_keywords():
    """Shared filler vocabulary should trip the advisory flag in
    _evaluate_planning_quality."""
    breakdown = {
        "tasks": [
            {
                "title": "placeholder task",
                "goal": "待补充",
                "acceptance_criteria": ["x"],
                "files": ["foo.py"],
                "evidence": "recon: 占位示例",
            }
        ]
    }
    blocking, advisory = aw._evaluate_planning_quality("demo", breakdown)
    assert any("placeholder-like" in msg for msg in advisory)
    # Blocking is expected here because the single produced task also means
    # "all tasks" are placeholder-like. Matches pre-refactor behavior.
    assert blocking, "all-placeholder breakdown should escalate to blocking"


def test_planner_quality_gate_flags_all_tasks_without_evidence_as_fabrication():
    breakdown = {
        "tasks": [
            {
                "title": "给 status 加 --json 参数",
                "goal": "输出合法 JSON",
                "acceptance_criteria": ["pytest 通过"],
                "files": ["codepilot/commands/status.py"],
                "evidence": "",
            },
            {
                "title": "给 history 清理接口",
                "goal": "支持清空",
                "acceptance_criteria": ["有 /clear 接口"],
                "files": ["codepilot/commands/history.py"],
                # evidence missing entirely
            },
        ]
    }
    blocking, advisory = aw._evaluate_planning_quality("demo", breakdown)
    assert any("fabricating" in msg for msg in blocking)
    assert sum(1 for msg in advisory if "empty `evidence`" in msg) == 2


def test_planner_quality_gate_accepts_concrete_task():
    breakdown = {
        "tasks": [
            {
                "title": "给 status 加 --json 参数",
                "goal": "输出合法 JSON",
                "acceptance_criteria": ["pytest -q tests/test_status.py 通过"],
                "files": ["codepilot/commands/status.py"],
                "evidence": "recon: status.py 当前没有 --json 分支",
            }
        ]
    }
    blocking, advisory = aw._evaluate_planning_quality("demo", breakdown)
    assert blocking == []
    assert not any("placeholder-like" in msg for msg in advisory)
    assert not any("evidence" in msg for msg in advisory)


# ─── inspect still uses the inspect-specific list ───────────────────────────


def test_inspect_filter_keeps_real_marker_reference_with_grounded_evidence():
    """Regression check: the inspect filter keeps legitimate marker
    references after the refactor routes it through task_quality."""
    signal_results = [
        inspect_cmd.InspectSignalResult(
            key="todos",
            title="代码里的 TODO/FIXME/XXX",
            order=3,
            enabled=True,
            content="codepilot/foo.py:42: TODO handle timeout in stream reader",
        ),
    ]
    candidate = {
        "title": "修复 foo.py 的 TODO",
        "goal": "处理 codepilot/foo.py:42 的 TODO，确保下游读完整数据。",
        "priority": "P3",
        "rationale": "注释标记指向明确行。",
        "kind": "bug",
        "evidence": "signal 3: codepilot/foo.py:42 TODO handle timeout",
        "effort": "small",
    }

    kept, dropped = inspect_cmd._filter_candidates([candidate], signal_results=signal_results)
    assert len(kept) == 1
    assert dropped == []
