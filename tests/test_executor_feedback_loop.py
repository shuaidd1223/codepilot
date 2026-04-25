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


def test_split_phase_helpers_preserve_runtime_phase_log_phase_and_review_bounds(
    fake_project,
    monkeypatch,
    tmp_path,
):
    """Builder/reviewer helpers keep the pre-split visible phase semantics."""
    from codepilot import progress_bus

    calls: list[dict] = []
    logs: list[dict] = []

    def fake_run_builtin_phase(**kwargs):
        calls.append(kwargs)
        label = "codex" if kwargs["phase"] == "builder" else "codex-review"
        return label, 0, f"{kwargs['phase']} output"

    monkeypatch.setattr(run_mod, "_run_builtin_phase", fake_run_builtin_phase)
    monkeypatch.setattr(
        run_mod,
        "_write_task_log",
        lambda task_id, agent, phase, output, exit_code, started_at: logs.append(
            {
                "task_id": task_id,
                "agent": agent,
                "phase": phase,
                "output": output,
                "exit_code": exit_code,
            }
        ),
    )
    monkeypatch.setattr(
        run_mod,
        "_git_changed_files",
        lambda project_path: ["codepilot/commands/run.py", "unexpected.txt"],
    )

    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")
    ctx = run_mod._ExecutorContext(
        task=task,
        project=fake_project,
        project_path=Path(fake_project["path"]),
        config_ref=None,
        output_dir=tmp_path,
        task_file=task_file,
        max_rounds=3,
        task_id_for_events=task["id"],
    )

    progress_bus.clear_subscribers_for_tests()
    events: list[dict] = []
    with progress_bus.subscription(events.append):
        builder = run_mod._run_builder_round(ctx, round_num=2, previous_findings="fix the failed AC")
        reviewer = run_mod._run_reviewer_round(ctx, round_num=2, previous_findings="fix the failed AC")

    assert builder.output == "builder output"
    assert reviewer.output == "reviewer output"

    assert calls[0]["phase"] == "builder"
    assert calls[0]["display_phase"] == "builder r2/3"
    assert "第 2 轮重做" in calls[0]["prompt"]
    assert "fix the failed AC" in calls[0]["prompt"]

    assert calls[1]["phase"] == "reviewer"
    assert calls[1]["display_phase"] == "reviewer r2/3"
    assert "codepilot/commands/run.py" in calls[1]["prompt"]
    assert "unexpected.txt" in calls[1]["prompt"]
    assert "越界修改" in calls[1]["prompt"]
    assert "fix the failed AC" in calls[1]["prompt"]

    assert [entry["phase"] for entry in logs] == ["builder-r2", "reviewer-r2"]
    # ``stage`` carries the per-round display label (e.g. "builder r2/3") so
    # the Web UI can fold each round independently; the canonical phase
    # identity lives in ``extra.phase_kind``. Each round emits at least
    # ``phase_start`` and ``phase_end`` events, so filter to phase_start to
    # assert builder/reviewer ordering.
    starts = [
        event for event in events
        if (event.get("extra") or {}).get("phase_kind") in {"builder", "reviewer"}
        and event.get("type") == "phase_start"
    ]
    assert [event["extra"]["phase_kind"] for event in starts[:2]] == ["builder", "reviewer"]
    assert [event["stage"] for event in starts[:2]] == ["builder r2/3", "reviewer r2/3"]
    for event in starts[:2]:
        assert event["extra"]["round"] == 2
        assert event["extra"]["round_total"] == 3


def test_reviewer_round_continues_when_changed_file_detection_fails(
    fake_project,
    monkeypatch,
    tmp_path,
):
    """A broken git diff/boundary probe must not skip the reviewer phase."""
    captured: dict = {}

    def fake_run_builtin_phase(**kwargs):
        captured.update(kwargs)
        return "codex-review", 0, "review ok"

    monkeypatch.setattr(run_mod, "_run_builtin_phase", fake_run_builtin_phase)
    monkeypatch.setattr(run_mod, "_write_task_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        run_mod,
        "_git_changed_files",
        lambda project_path: (_ for _ in ()).throw(RuntimeError("diff failed")),
    )

    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")
    ctx = run_mod._ExecutorContext(
        task=task,
        project=fake_project,
        project_path=Path(fake_project["path"]),
        config_ref=None,
        output_dir=tmp_path,
        task_file=task_file,
        max_rounds=2,
        task_id_for_events=task["id"],
    )

    outcome = run_mod._run_reviewer_round(ctx, round_num=1)

    assert outcome.exit_code == 0
    assert captured["phase"] == "reviewer"
    assert "diff failed" not in captured["prompt"]
    assert "本次 builder 实际改动的文件" not in captured["prompt"]


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


def test_run_builtin_executor_delegates_loop_and_result_mapping(fake_project, monkeypatch, tmp_path):
    """Executor entrypoint should orchestrate via loop + mapper helpers."""
    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")

    monkeypatch.setattr(run_mod, "_builtin_preflight_error", lambda *a, **kw: "")
    monkeypatch.setattr(run_mod, "_builtin_runtime_dir", lambda project: tmp_path / "runtime")

    captured: dict[str, object] = {}
    fake_outcome = run_mod._BuiltinLoopOutcome(
        status="builder_error",
        round_num=1,
        builder=run_mod._PhaseOutcome(agent="codex", exit_code=1, output="boom"),
    )
    fake_result = run_mod.ExecutionResult(exit_code=1, output="mapped", executor="builtin")

    def fake_loop(ctx):
        captured["loop_ctx"] = ctx
        return fake_outcome

    def fake_mapper(ctx, outcome, *, auto_commit):
        captured["map_ctx"] = ctx
        captured["map_outcome"] = outcome
        captured["map_auto_commit"] = auto_commit
        return fake_result

    monkeypatch.setattr(run_mod, "_run_builtin_round_loop", fake_loop)
    monkeypatch.setattr(run_mod, "_map_builtin_loop_outcome", fake_mapper)

    result = run_mod._run_builtin_executor(
        task,
        fake_project,
        task_file,
        auto_commit=False,
        max_review_rounds=3,
    )

    assert result is fake_result
    assert captured["loop_ctx"] is captured["map_ctx"]
    assert captured["map_outcome"] is fake_outcome
    assert captured["map_auto_commit"] is False


def test_map_builtin_loop_outcome_exhausted_is_deterministic(fake_project, tmp_path):
    """Exhausted review rounds should map to deterministic failure result."""
    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")
    ctx = run_mod._ExecutorContext(
        task=task,
        project=fake_project,
        project_path=Path(fake_project["path"]),
        config_ref=None,
        output_dir=tmp_path,
        task_file=task_file,
        max_rounds=2,
        task_id_for_events=task["id"],
    )
    outcome = run_mod._BuiltinLoopOutcome(
        status="exhausted",
        round_num=2,
        verdict="unknown",
        builder=run_mod._PhaseOutcome(agent="codex", exit_code=0, output="builder out"),
        reviewer=run_mod._PhaseOutcome(agent="codex-review", exit_code=0, output="review out"),
    )

    result = run_mod._map_builtin_loop_outcome(ctx, outcome, auto_commit=False)

    assert result.exit_code == 2
    assert result.deterministic_failure is True
    assert "review 结果不明确" in (result.summary or "")
    assert result.output == "builder out"
    assert result.review_output == "review out"


def test_map_builtin_loop_outcome_uses_loop_provided_terminal_fields(fake_project, tmp_path):
    """Loop-owned terminal summary/flags should pass through mapping unchanged."""
    task = _sample_task()
    task_file = tmp_path / "42-task.md"
    task_file.write_text("stub", encoding="utf-8")
    ctx = run_mod._ExecutorContext(
        task=task,
        project=fake_project,
        project_path=Path(fake_project["path"]),
        config_ref=None,
        output_dir=tmp_path,
        task_file=task_file,
        max_rounds=2,
        task_id_for_events=task["id"],
    )
    outcome = run_mod._BuiltinLoopOutcome(
        status="reviewer_error",
        round_num=1,
        summary="reviewer transport failed",
        builder=run_mod._PhaseOutcome(agent="codex", exit_code=0, output="builder out"),
        reviewer=run_mod._PhaseOutcome(agent="codex-review", exit_code=7, output="review out"),
    )

    result = run_mod._map_builtin_loop_outcome(ctx, outcome, auto_commit=False)

    assert result.exit_code == 7
    assert result.summary == "reviewer transport failed"
    assert result.output == "builder out"
    assert result.review_output == "review out"


def test_config_threads_max_review_rounds():
    """AutomationConfig should expose max_review_rounds with a sane default."""
    from codepilot.config import AutomationConfig
    cfg = AutomationConfig()
    assert cfg.max_review_rounds == 2
