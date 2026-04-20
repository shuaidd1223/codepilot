"""Tests for the enriched builder/reviewer prompts and the FAIL→retry loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.commands import run as run_mod


# ─── prompt helpers ─────────────────────────────────────────────────────────


SAMPLE_CONTENT = """\
## 任务目标

让 /history 能持久化。

## 验收标准

- 重启后仍能看到
- 支持清空历史

## Builder 职责

- 修改 auto_chat.py
- 新增 session 表

## Reviewer 职责

- 检查 SQL 注入

## 涉及文件

- codepilot/commands/auto_chat.py
"""


def _sample_task() -> dict:
    return {"id": 42, "title": "给 chat 加会话历史", "content": SAMPLE_CONTENT}


def test_extract_task_sections_splits_markdown_headings():
    sections = run_mod._extract_task_sections(SAMPLE_CONTENT)
    assert set(sections) >= {"任务目标", "验收标准", "Builder 职责", "涉及文件"}
    assert "持久化" in sections["任务目标"]


def test_bullet_lines_strips_placeholders():
    body = "- 待补充\n- 真的事情\n- 无"
    assert run_mod._bullet_lines(body) == ["真的事情"]


def test_builder_prompt_round1_lists_acceptance_criteria(tmp_path):
    prompt = run_mod._build_builtin_prompt(
        _sample_task(), tmp_path / "42-task.md", project_path=tmp_path,
    )
    # Criteria enumerated inline so builder can cross-check.
    assert "验收标准" in prompt
    assert "重启后仍能看到" in prompt
    assert "支持清空历史" in prompt
    # Builder notes surfaced.
    assert "修改 auto_chat.py" in prompt
    # Round-1 wording.
    assert "第 2 轮" not in prompt


def test_builder_prompt_round2_includes_previous_feedback(tmp_path):
    feedback = "1. /history 重启后丢失\n2. 没有清空按钮"
    prompt = run_mod._build_builtin_prompt(
        _sample_task(),
        tmp_path / "42-task.md",
        project_path=tmp_path,
        review_round=2,
        previous_review_feedback=feedback,
    )
    assert "第 2 轮重做" in prompt
    assert "/history 重启后丢失" in prompt
    assert "没有清空按钮" in prompt


def test_builder_prompt_surfaces_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Agent rules\n- 每次改动都要跑 pytest 并附结果。", encoding="utf-8",
    )
    prompt = run_mod._build_builtin_prompt(
        _sample_task(), tmp_path / "42-task.md", project_path=tmp_path,
    )
    assert "项目约定" in prompt
    assert "每次改动都要跑 pytest" in prompt


def test_reviewer_prompt_enumerates_acceptance_criteria():
    prompt = run_mod._build_review_prompt(_sample_task())
    assert "必须逐条核对" in prompt
    assert "重启后仍能看到" in prompt
    assert "支持清空历史" in prompt
    # Must produce a clear VERDICT line at the end.
    assert "VERDICT: PASS" in prompt
    assert "VERDICT: FAIL" in prompt


def test_reviewer_prompt_includes_reviewer_notes():
    prompt = run_mod._build_review_prompt(_sample_task())
    assert "SQL 注入" in prompt


# ─── reviewer-findings extractor ───────────────────────────────────────────


def test_extract_reviewer_findings_prefers_needs_fix_section():
    raw = (
        "整体审查如下...\n"
        "需要修复的点:\n"
        "1. /history 重启后丢失\n"
        "2. 没有清空按钮\n"
        "\n"
        "VERDICT: FAIL"
    )
    out = run_mod._extract_reviewer_findings(raw)
    assert "1. /history 重启后丢失" in out
    assert "2. 没有清空按钮" in out
    assert "VERDICT" not in out


def test_extract_reviewer_findings_falls_back_to_everything_but_verdict():
    raw = "自由散文说明原因。\n可能是 X 导致的。\nVERDICT: FAIL"
    out = run_mod._extract_reviewer_findings(raw)
    assert "VERDICT" not in out
    assert "自由散文" in out


def test_extract_reviewer_findings_empty_on_empty_input():
    assert run_mod._extract_reviewer_findings("") == ""


# ─── feedback loop inside _run_builtin_executor ────────────────────────────


class _PhaseRecorder:
    """Replace ai._phase_stub so we control builder/reviewer outcomes per round."""

    def __init__(self, scripted: list[tuple[str, str, int]]):
        """scripted = list of (phase, output, exit_code) tuples, consumed in order."""
        self.scripted = list(scripted)
        self.calls: list[dict] = []

    def __call__(self, *, task, project_path, phase, prompt):
        expected_phase, output, exit_code = self.scripted.pop(0)
        assert phase == expected_phase, (
            f"expected phase {expected_phase}, got {phase}; "
            f"remaining script: {self.scripted}"
        )
        self.calls.append({"phase": phase, "prompt": prompt})
        agent_label = "codex" if phase == "builder" else "codex-review"
        return agent_label, exit_code, output


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    """Minimal project folder + monkeypatches so the executor doesn't touch git."""
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    (proj_root / "README.md").write_text("# x", encoding="utf-8")

    # Skip git preflight & commit machinery.
    monkeypatch.setattr(run_mod, "_builtin_preflight_error", lambda *a, **kw: "")
    monkeypatch.setattr(run_mod, "_git_auto_commit", lambda *a, **kw: "abc123")
    monkeypatch.setattr(run_mod, "_write_task_log", lambda *a, **kw: None)

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setattr(run_mod, "_builtin_runtime_dir", lambda project: runtime_dir)

    return {
        "name": "demo",
        "path": str(proj_root),
        "config_file": None,
    }


def test_review_loop_retries_until_pass(fake_project, monkeypatch, tmp_path):
    """First reviewer says FAIL, builder retries, second reviewer PASS → done."""
    from codepilot import ai as ai_mod

    recorder = _PhaseRecorder([
        ("builder", "first attempt output", 0),
        ("reviewer", "need to fix X\nVERDICT: FAIL", 0),
        ("builder", "second attempt output", 0),
        ("reviewer", "all good\nVERDICT: PASS", 0),
    ])
    monkeypatch.setattr(ai_mod, "_phase_stub", recorder)

    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")

    result = run_mod._run_builtin_executor(
        task, fake_project, task_file, auto_commit=True, max_review_rounds=3,
    )

    assert result.exit_code == 0
    assert "rounds=2" in (result.summary or "")
    # Builder r2 prompt must mention the prior FAIL feedback.
    builder_r2_prompt = recorder.calls[2]["prompt"]
    assert "第 2 轮重做" in builder_r2_prompt
    assert "need to fix X" in builder_r2_prompt


def test_review_loop_exhausts_rounds_and_fails(fake_project, monkeypatch, tmp_path):
    """Reviewer keeps returning FAIL → result exit_code=2, summary says exhausted."""
    from codepilot import ai as ai_mod

    recorder = _PhaseRecorder([
        ("builder", "b1", 0),
        ("reviewer", "broken\nVERDICT: FAIL", 0),
        ("builder", "b2", 0),
        ("reviewer", "still broken\nVERDICT: FAIL", 0),
    ])
    monkeypatch.setattr(ai_mod, "_phase_stub", recorder)

    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")

    result = run_mod._run_builtin_executor(
        task, fake_project, task_file, auto_commit=False, max_review_rounds=2,
    )

    assert result.exit_code == 2
    assert "用完重做轮次" in (result.summary or "")
    # deterministic_failure 让调用方跳过任务级 retry，避免同样的 FAIL 再烧一遍 token
    assert result.deterministic_failure is True


def test_builder_crash_is_not_deterministic(fake_project, monkeypatch, tmp_path):
    """builder 非零退出是瞬时失败，仍允许任务级重试。"""
    from codepilot import ai as ai_mod

    recorder = _PhaseRecorder([("builder", "boom", 1)])
    monkeypatch.setattr(ai_mod, "_phase_stub", recorder)

    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")

    result = run_mod._run_builtin_executor(
        task, fake_project, task_file, auto_commit=False, max_review_rounds=2,
    )

    assert result.exit_code != 0
    assert result.deterministic_failure is False


def test_review_loop_disabled_when_max_rounds_one(fake_project, monkeypatch, tmp_path):
    """max_review_rounds=1 reverts to legacy single-pass behavior."""
    from codepilot import ai as ai_mod

    recorder = _PhaseRecorder([
        ("builder", "b1", 0),
        ("reviewer", "broken\nVERDICT: FAIL", 0),
    ])
    monkeypatch.setattr(ai_mod, "_phase_stub", recorder)

    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")

    result = run_mod._run_builtin_executor(
        task, fake_project, task_file, auto_commit=False, max_review_rounds=1,
    )

    assert result.exit_code == 2
    # Only 2 calls total (one builder + one reviewer), no retry.
    assert len(recorder.calls) == 2


def test_review_loop_stops_on_first_pass(fake_project, monkeypatch, tmp_path):
    """Reviewer PASS on round 1 → exactly 2 phases, auto-commit triggered."""
    from codepilot import ai as ai_mod

    recorder = _PhaseRecorder([
        ("builder", "b1", 0),
        ("reviewer", "great job\nVERDICT: PASS", 0),
    ])
    monkeypatch.setattr(ai_mod, "_phase_stub", recorder)

    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")

    result = run_mod._run_builtin_executor(
        task, fake_project, task_file, auto_commit=True, max_review_rounds=3,
    )

    assert result.exit_code == 0
    assert len(recorder.calls) == 2
    assert "rounds=1" in (result.summary or "")
    assert "commit: abc123" in (result.summary or "")


def test_config_threads_max_review_rounds():
    """AutomationConfig should expose max_review_rounds with a sane default."""
    from codepilot.config import AutomationConfig
    cfg = AutomationConfig()
    assert cfg.max_review_rounds == 2
