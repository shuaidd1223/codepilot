"""Task-spec normalization and fallback-breakdown schema coverage.

When the real planner output skips risk_level / scope_budget, or when we
fall back to the single-task shim, the downstream template must still
render with meaningful values instead of "待评估 / 未设定" placeholders.
"""

from __future__ import annotations


from codepilot.commands import auto_workflow as aw


def test_fallback_single_task_breakdown_populates_required_schema_fields():
    breakdown = aw._fallback_single_task_breakdown(
        title="修复登录闪退",
        priority="P1",
        exc=RuntimeError("codex 超时"),
    )

    assert breakdown["complexity"] == "simple"
    assert breakdown["should_split"] is False
    assert len(breakdown["tasks"]) == 1

    task = breakdown["tasks"][0]
    # Original fields still present.
    assert task["title"] == "修复登录闪退"
    assert task["priority"] == "P1"
    assert task["goal"] == "修复登录闪退"
    assert isinstance(task["acceptance_criteria"], list) and task["acceptance_criteria"]
    assert isinstance(task["files"], list)
    # New required schema fields must be present with sane defaults.
    assert task["depends_on_indices"] == []
    assert task["risk_level"] == "medium"
    assert task["scope_budget"] == "unplanned / single-task fallback"
    assert "fallback mode" in task["evidence"]
    # Fallback reason surfaced in notes.
    assert any("codex 超时" in note for note in task["notes"])


def test_normalize_task_spec_preserves_and_strips_evidence():
    item = {"evidence": "  recon: status.py 没有 --json 分支  "}
    aw._normalize_task_spec(item)
    assert item["evidence"] == "recon: status.py 没有 --json 分支"


def test_normalize_task_spec_empty_evidence_stays_empty_for_gate_detection():
    """Quality gate relies on empty-string detection; don't auto-fill evidence."""
    item = {}
    aw._normalize_task_spec(item)
    assert item["evidence"] == ""


def test_normalize_task_spec_fills_missing_risk_and_budget_with_defaults():
    item = {
        "title": "demo",
        "goal": "goal",
    }
    aw._normalize_task_spec(item)

    assert item["risk_level"] == "medium"
    assert item["scope_budget"] == "unspecified"
    assert item["depends_on_indices"] == []


def test_normalize_task_spec_keeps_valid_risk_level_casing():
    item = {"risk_level": "HIGH", "scope_budget": "2 files / ~40 LOC"}
    aw._normalize_task_spec(item)
    assert item["risk_level"] == "high"
    assert item["scope_budget"] == "2 files / ~40 LOC"


def test_normalize_task_spec_clamps_invalid_risk_level_to_medium():
    item = {"risk_level": "critical"}  # not in enum
    aw._normalize_task_spec(item)
    assert item["risk_level"] == "medium"


def test_normalize_task_spec_sanitizes_bad_depends_on_indices():
    item = {"depends_on_indices": ["0", 1, -3, None, 2]}
    aw._normalize_task_spec(item)
    assert item["depends_on_indices"] == [1, 2]


def test_normalize_task_spec_empty_budget_coerced_to_unspecified():
    item = {"scope_budget": "   "}
    aw._normalize_task_spec(item)
    assert item["scope_budget"] == "unspecified"


def test_normalize_task_spec_passthrough_for_non_dict():
    assert aw._normalize_task_spec("not a dict") == "not a dict"


def test_build_task_markdown_fallback_renders_without_placeholder_defaults():
    """End-to-end: a fallback breakdown item rendered via the template should
    NOT show the template-level fallback literals like 待评估/未设定."""
    from codepilot.ai_support.service import build_task_markdown_from_plan

    breakdown = aw._fallback_single_task_breakdown(
        title="验证 fallback 渲染",
        priority="P2",
        exc=RuntimeError("planning timeout"),
    )
    task = breakdown["tasks"][0]
    task["agent"] = "dual"

    md = build_task_markdown_from_plan(task)

    # The template defaults 待评估/未设定/未指派 should NOT appear for risk/budget
    # because the fallback populated them explicitly.
    assert "Risk Level | medium" in md
    assert "Scope Budget | unplanned / single-task fallback" in md
    # Owner has no fallback-side source; template default still kicks in.
    assert "Owner | 未指派" in md
    # Evidence is populated by fallback — template default for unpopulated
    # evidence should NOT appear.
    assert "Planning Evidence" in md
    assert "fallback mode" in md
    assert "未提供规划依据" not in md


def test_create_tasks_from_breakdown_normalizes_planner_output(monkeypatch):
    """A planner item without risk_level/scope_budget should still render
    sensible defaults by running through _normalize_task_spec."""
    from codepilot.ai_support.service import build_task_markdown_from_plan

    captured_contents: list[str] = []

    def _fake_create_task(**kwargs):
        captured_contents.append(kwargs["content"])
        return {"id": len(captured_contents), **kwargs}

    monkeypatch.setattr(aw.db, "create_task", _fake_create_task)

    breakdown = {
        "tasks": [
            {
                "title": "第一个任务",
                "goal": "goal",
                "priority": "P2",
                "acceptance_criteria": ["必须跑通"],
                "builder_notes": ["实现 x"],
                "reviewer_notes": ["看 x"],
                "files": [],
                "notes": [],
                # risk_level / scope_budget / depends_on_indices intentionally missing
            }
        ]
    }

    tasks = aw._create_tasks_from_breakdown(
        breakdown=breakdown,
        project_name="demo",
        project_path="/tmp/demo",
        task_agent="dual",
        priority="P3",
        max_retries=0,
    )

    assert len(tasks) == 1
    rendered = captured_contents[0]
    # Normalizer defaults made their way into the rendered template.
    assert "Risk Level | medium" in rendered
    assert "Scope Budget | unspecified" in rendered
    # Missing evidence falls back to the template-level "未提供规划依据" notice
    # so a human reader can see the task needs human review.
    assert "未提供规划依据" in rendered
    # Sanity: smoke build_task_markdown_from_plan is still live alongside.
    assert "第一个任务" in rendered
    assert build_task_markdown_from_plan is not None

