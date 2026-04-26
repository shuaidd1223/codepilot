"""Tests for planner context / clarification / two-stage planner."""

from __future__ import annotations

import sys

import pytest

from codepilot.ai_support import service as ai_mod
from codepilot.ai_support import backlog_dedup as dedup_mod
from codepilot.ai_support import clarify as ai_clarify
from codepilot.ai_support import planner_context as ctx_mod


def _q(text: str, *, qid: str = "q1") -> dict:
    return {
        "id": qid,
        "type": "text",
        "text": text,
        "options": [],
        "allow_free_text": False,
    }


# ─── ai_planner_context ────────────────────────────────────────────────────


def test_extract_keywords_handles_mixed_cn_en():
    kws = ctx_mod._extract_keywords("给 webui 加会话历史 sidebar")
    assert "webui" in kws
    assert "sidebar" in kws
    # Keep CJK bigrams too so file-path matching works against Chinese tokens.
    assert "会话" in kws or "会话历史" in kws


def test_extract_keywords_drops_stopwords():
    kws = ctx_mod._extract_keywords("帮我 add dark mode 优化")
    # 'add' is in stopword list, shouldn't come back as-is.
    assert "add" not in kws
    assert "dark" in kws
    assert "mode" in kws


def test_collect_planner_context_embeds_readme_and_tree(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Demo Project\nThis is a demo.", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("x=1", encoding="utf-8")
    (tmp_path / "src" / "beta.py").write_text("x=1", encoding="utf-8")

    ctx = ctx_mod.collect_planner_context(str(tmp_path), "tweak alpha")

    assert "README" in ctx
    assert "Demo Project" in ctx
    assert "src" in ctx
    assert "pyproject.toml" in ctx
    assert "alpha" in ctx  # matched via keyword
    # token budget enforced
    assert len(ctx) < 5000


def test_collect_planner_context_empty_for_missing_path():
    assert ctx_mod.collect_planner_context("", "x") == ""
    assert ctx_mod.collect_planner_context("/does/not/exist-xyz-xyz", "x") == ""


# ─── ai_planner_context: conventions reader ───────────────────────────────


def test_read_project_conventions_picks_up_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Agent rules\n- Every PR must add tests.\n- Never modify auth.py.",
        encoding="utf-8",
    )
    out = ctx_mod._read_project_conventions(tmp_path)
    assert "Every PR must add tests" in out
    assert "AGENTS.md" in out


def test_read_project_conventions_concatenates_multiple_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Rule A", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Rule C", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("Rule T", encoding="utf-8")
    out = ctx_mod._read_project_conventions(tmp_path)
    assert "Rule A" in out
    assert "Rule C" in out
    assert "Rule T" in out


def test_read_project_conventions_caps_per_file_and_total(tmp_path):
    big_body = "x" * 10_000
    (tmp_path / "AGENTS.md").write_text(big_body, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text(big_body, encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text(big_body, encoding="utf-8")
    out = ctx_mod._read_project_conventions(tmp_path, max_chars_per_file=500, total_cap=1200)
    assert len(out) <= 1200 + 40  # small slack for truncation suffix
    assert "truncated" in out or "cap reached" in out


def test_read_project_conventions_returns_empty_when_no_files(tmp_path):
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    assert ctx_mod._read_project_conventions(tmp_path) == ""


def test_collect_planner_context_surfaces_conventions(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Agent rules\n- run `pytest` before every commit.", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# Demo", encoding="utf-8")
    ctx = ctx_mod.collect_planner_context(str(tmp_path), "demo task")
    assert "Project Conventions" in ctx
    assert "pytest" in ctx


# ─── ai_planner_context: recon validator ──────────────────────────────────


def test_validate_recon_payload_drops_nonexistent(tmp_path):
    (tmp_path / "real.py").write_text("x=1", encoding="utf-8")
    payload = {
        "relevant_files": ["real.py", "ghost.py", "sub/not_here.py"],
        "current_state": "x",
    }
    cleaned, dropped = ctx_mod.validate_recon_payload(payload, str(tmp_path))
    assert cleaned["relevant_files"] == ["real.py"]
    assert set(dropped) == {"ghost.py", "sub/not_here.py"}


def test_validate_recon_payload_backfills_from_keywords(tmp_path):
    # Create some real files so the keyword fallback finds matches.
    (tmp_path / "auth.py").write_text("x", encoding="utf-8")
    (tmp_path / "auth_helpers.py").write_text("x", encoding="utf-8")
    (tmp_path / "other.py").write_text("x", encoding="utf-8")

    # Planner returned only ghost files.
    cleaned, dropped = ctx_mod.validate_recon_payload(
        {"relevant_files": ["ghost.py", "imaginary/other.py"]},
        str(tmp_path),
        title="auth flow refactor",
    )
    # Hallucinations gone, real auth files filled in.
    assert "ghost.py" not in cleaned["relevant_files"]
    assert any("auth" in p for p in cleaned["relevant_files"])
    assert dropped == ["ghost.py", "imaginary/other.py"]


def test_validate_recon_payload_preserves_non_file_fields(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    payload = {
        "relevant_files": ["a.py"],
        "current_state": "existing",
        "key_findings": ["f1"],
        "risks": ["r1"],
        "suggested_approach": "do X",
    }
    cleaned, _ = ctx_mod.validate_recon_payload(payload, str(tmp_path))
    assert cleaned["current_state"] == "existing"
    assert cleaned["key_findings"] == ["f1"]
    assert cleaned["risks"] == ["r1"]
    assert cleaned["suggested_approach"] == "do X"


def test_validate_recon_payload_handles_garbage_input(tmp_path):
    # Non-dict → empty dict.
    assert ctx_mod.validate_recon_payload(None, str(tmp_path)) == ({}, [])
    # Non-string entries in the list are ignored, not raised on.
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    cleaned, dropped = ctx_mod.validate_recon_payload(
        {"relevant_files": ["a.py", 42, None, ""]},
        str(tmp_path),
    )
    assert cleaned["relevant_files"] == ["a.py"]
    assert dropped == []


def test_validate_recon_payload_normalizes_backslashes(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x", encoding="utf-8")
    cleaned, dropped = ctx_mod.validate_recon_payload(
        {"relevant_files": ["pkg\\mod.py", "./pkg/mod.py"]},
        str(tmp_path),
    )
    # Both normalize to pkg/mod.py, dedup to one entry.
    assert cleaned["relevant_files"] == ["pkg/mod.py"]
    assert dropped == []


# ─── validator integration with the two-stage planner ────────────────────


def test_generate_task_breakdown_filters_hallucinated_recon_files(tmp_path, monkeypatch):
    """When recon lists files that don't exist, the planner prompt must not
    carry them forward — the validator strips them first."""
    (tmp_path / "real_mod.py").write_text("x=1", encoding="utf-8")

    planner_prompt_text = {}

    def fake_claude(prompt, schema, **kwargs):
        if "current_state" in schema.get("properties", {}):
            return {
                "current_state": "x",
                "relevant_files": [
                    "real_mod.py",              # exists
                    "ghost_mod.py",             # doesn't
                    "path/to/fantasy.py",       # doesn't
                ],
                "key_findings": [],
                "risks": [],
                "suggested_approach": "do it",
            }
        planner_prompt_text["text"] = prompt
        return {
            "summary": "x",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "touch real_mod",
                    "priority": "P2",
                    "goal": "do it",
                    "acceptance_criteria": ["a", "b"],
                    "builder_notes": [],
                    "reviewer_notes": [],
                    "files": [],
                    "notes": [],
                    "depends_on_indices": [],
                }
            ],
        }

    monkeypatch.setattr(ai_mod, "_run_claude_schema_prompt", fake_claude)

    ai_mod.generate_task_breakdown(
        "refactor real_mod",
        project_path=str(tmp_path),
        planner="claude",
        two_stage=True,
    )

    text = planner_prompt_text["text"]
    assert "real_mod.py" in text
    assert "ghost_mod.py" not in text
    assert "fantasy.py" not in text


# ─── ai_clarify heuristic ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title,expected_vague",
    [
        ("优化一下", True),
        ("优化", True),
        ("修 bug", True),
        ("改进用户体验", True),
        ("给 webui 加会话历史 sidebar", False),
        ("重构 codepilot/webui.py 为组件化", False),
        ("让 chat 支持多轮澄清", False),
        ("提升 cli 启动速度", False),
        ("add dark mode", False),
        ("optimize", True),
    ],
)
def test_heuristic_needs_clarification(title, expected_vague):
    assert ai_clarify.heuristic_needs_clarification(title) is expected_vague


def test_merge_clarification_history_concatenates_rounds():
    merged = ai_clarify.merge_clarification_history(
        "优化一下",
        [
            {"question": "优化什么?", "answer": "webui 加载速度"},
            {"question": "具体哪个页面?", "answer": "任务列表"},
        ],
    )
    assert "优化一下" in merged
    assert "webui" in merged
    assert "任务列表" in merged


def test_assess_requirement_skips_ai_when_heuristic_is_happy(monkeypatch):
    """Concrete requirement should be 'ready' immediately, no AI call."""
    calls = {"count": 0}

    def _should_not_run(*a, **kw):
        calls["count"] += 1
        raise AssertionError("AI should not be consulted for specific requirements")

    monkeypatch.setattr(ai_clarify, "_invoke_clarifier_ai", _should_not_run)

    result = ai_clarify.assess_requirement(
        "给 webui 加会话历史 sidebar",
        project_path="",
    )
    assert result["status"] == "ready"
    assert result["source"] == "heuristic"
    assert calls["count"] == 0


def test_assess_requirement_consults_ai_for_broad_non_vague_input(monkeypatch):
    calls = {"count": 0}

    def _fake_ai(*a, **kw):
        calls["count"] += 1
        return {
            "status": "needs_clarification",
            "questions": [_q("你希望先覆盖哪些模块?")],
        }

    monkeypatch.setattr(ai_clarify, "_invoke_clarifier_ai", _fake_ai)

    result = ai_clarify.assess_requirement(
        "做一个全自动编程工作流智能体",
        project_path="",
    )
    assert result["status"] == "needs_clarification"
    assert calls["count"] == 1


def test_assess_requirement_forces_ready_after_max_turns(monkeypatch):
    """After max_turns rounds we stop asking and plan with what we have."""
    monkeypatch.setattr(
        ai_clarify, "_invoke_clarifier_ai",
        lambda *a, **kw: {"status": "needs_clarification", "questions": [_q("again?")]},
    )
    result = ai_clarify.assess_requirement(
        "优化一下",
        qa_history=[
            {"question": "优化什么?", "answer": "webui"},
            {"question": "哪个模块?", "answer": "任务列表"},
            {"question": "指标?", "answer": "首屏 < 1s"},
        ],
        max_turns=3,
    )
    assert result["status"] == "ready"
    assert result["source"] == "forced"


def test_assess_requirement_relays_ai_questions(monkeypatch):
    monkeypatch.setattr(
        ai_clarify, "_invoke_clarifier_ai",
        lambda *a, **kw: {
            "status": "needs_clarification",
            "questions": [_q("优化谁?", qid="scope"), _q("目标指标?", qid="metric")],
            "reason": "太宽泛",
        },
    )
    result = ai_clarify.assess_requirement("优化一下", project_path="")
    assert result["status"] == "needs_clarification"
    assert result["questions"] == [_q("优化谁?", qid="scope"), _q("目标指标?", qid="metric")]
    assert result["source"] == "ai"
    assert result["turn"] == 1


def test_assess_requirement_falls_back_to_ready_on_ai_error(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("no API key")

    monkeypatch.setattr(ai_clarify, "_invoke_clarifier_ai", _boom)
    result = ai_clarify.assess_requirement("优化一下", project_path="")
    assert result["status"] == "ready"
    assert result["source"] == "ai-error"


def test_assess_requirement_passes_config_ref_to_gateway(monkeypatch):
    from codepilot.gateway import service as ai_gateway
    from codepilot.gateway.service import GatewayResponse

    captured: dict[str, str] = {}

    def _fake_gateway(request):
        captured["project_path"] = request.project_path
        captured["config_ref"] = request.config_ref
        captured["planner"] = request.planner
        return GatewayResponse(
            ok=True,
            source="cli:claude",
            payload={"status": "ready", "refined_title": "优化 webui 启动"},
        )

    monkeypatch.setattr(ai_gateway, "call_structured", _fake_gateway)

    result = ai_clarify.assess_requirement(
        "优化一下",
        project_path="D:/demo/project",
        config_ref="D:/demo/config/AGENTS.toml",
        planner="claude",
    )

    assert result["status"] == "ready"
    assert captured["project_path"] == "D:/demo/project"
    assert captured["config_ref"] == "D:/demo/config/AGENTS.toml"
    assert captured["planner"] == "claude"


# ─── two-stage generate_task_breakdown ─────────────────────────────────────


def test_generate_task_breakdown_two_stage_passes_recon_to_planner(tmp_path, monkeypatch):
    """When two_stage=True, the recon output must end up in the planner prompt."""
    (tmp_path / "README.md").write_text("# Demo", encoding="utf-8")
    # Make the paths recon returns real, otherwise validate_recon_payload
    # (correctly) strips them as hallucinations.
    (tmp_path / "codepilot").mkdir()
    (tmp_path / "codepilot" / "webui.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "codepilot" / "web").mkdir()
    (tmp_path / "codepilot" / "web" / "index.html").write_text("<html></html>", encoding="utf-8")

    recon_output = {
        "current_state": "现在 webui 是大 JS 文件",
        "relevant_files": ["codepilot/webui.py", "codepilot/web/index.html"],
        "key_findings": ["没有组件边界"],
        "risks": ["可能破坏 E2E 测试"],
        "suggested_approach": "抽 store + 按面板拆组件",
    }
    captured = {}

    def fake_claude(prompt, schema, **kwargs):
        # First call = recon (RECON_SCHEMA), second = breakdown.
        if "current_state" in schema.get("properties", {}):
            return recon_output
        captured["planner_prompt"] = prompt
        return {
            "summary": "拆 webui",
            "complexity": "complex",
            "should_split": True,
            "tasks": [
                {
                    "title": "抽取 store 模块",
                    "priority": "P1",
                    "goal": "新建 store.js 承载共享状态",
                    "acceptance_criteria": ["store.js 创建", "无测试回归"],
                    "builder_notes": ["阅读 webui.py"],
                    "reviewer_notes": ["检查 store api"],
                    "files": ["codepilot/web/store.js"],
                    "notes": [],
                    "depends_on_indices": [],
                },
            ],
        }

    monkeypatch.setattr(ai_mod, "_run_claude_schema_prompt", fake_claude)

    result = ai_mod.generate_task_breakdown(
        "把 webui 重构为组件化",
        project_path=str(tmp_path),
        planner="claude",
        max_tasks=3,
        two_stage=True,
    )

    assert result["tasks"][0]["title"] == "抽取 store 模块"
    assert "codepilot/webui.py" in captured["planner_prompt"]
    assert "抽 store + 按面板拆组件" in captured["planner_prompt"]


def test_generate_task_breakdown_single_stage_skips_recon(tmp_path, monkeypatch):
    """When two_stage=False, only one CLI call happens and no recon block shows up."""
    (tmp_path / "README.md").write_text("# x", encoding="utf-8")

    calls = []

    def fake_claude(prompt, schema, **kwargs):
        calls.append(schema.get("properties", {}))
        return {
            "summary": "do it",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "do the thing",
                    "priority": "P2",
                    "goal": "y",
                    "acceptance_criteria": ["a", "b"],
                    "builder_notes": [],
                    "reviewer_notes": [],
                    "files": [],
                    "notes": [],
                    "depends_on_indices": [],
                }
            ],
        }

    monkeypatch.setattr(ai_mod, "_run_claude_schema_prompt", fake_claude)

    result = ai_mod.generate_task_breakdown(
        "do the thing concretely please",
        project_path=str(tmp_path),
        planner="claude",
        two_stage=False,
    )
    assert result["tasks"][0]["title"] == "do the thing"
    # Exactly one CLI call (no recon stage).
    assert len(calls) == 1
    # The single call was the breakdown schema, not recon.
    assert "current_state" not in calls[0]


def test_generate_task_breakdown_tolerates_recon_failure(tmp_path, monkeypatch):
    """If recon raises, planning should still run with empty recon block."""
    (tmp_path / "README.md").write_text("# x", encoding="utf-8")
    planner_prompt = {}

    def fake_claude(prompt, schema, **kwargs):
        if "current_state" in schema.get("properties", {}):
            raise RuntimeError("recon broke")
        planner_prompt["text"] = prompt
        return {
            "summary": "x",
            "complexity": "simple",
            "should_split": False,
            "tasks": [
                {
                    "title": "do x",
                    "priority": "P2",
                    "goal": "g",
                    "acceptance_criteria": ["a", "b"],
                    "builder_notes": [],
                    "reviewer_notes": [],
                    "files": [],
                    "notes": [],
                    "depends_on_indices": [],
                },
            ],
        }

    monkeypatch.setattr(ai_mod, "_run_claude_schema_prompt", fake_claude)
    result = ai_mod.generate_task_breakdown(
        "refactor",
        project_path=str(tmp_path),
        planner="claude",
        two_stage=True,
    )
    assert result["tasks"]
    # The planner ran and its prompt was built with an empty recon block.
    assert (
        "No recon conclusions were produced" in planner_prompt["text"]
        or "Recon output is empty" in planner_prompt["text"]
    )


# ─── ai_backlog_dedup ─────────────────────────────────────────────────────


def test_format_existing_block_filters_to_open_tasks():
    rows = [
        {"id": 1, "title": "已完成的不要秀", "status": "done"},
        {"id": 2, "title": "backlog 里的活", "status": "backlog"},
        {"id": 3, "title": "正在做的", "status": "in_progress"},
        {"id": 4, "title": "被取消的不要秀", "status": "cancelled"},
    ]
    out = dedup_mod.format_existing_block(rows)
    assert "#2" in out
    assert "#3" in out
    assert "#1" not in out
    assert "#4" not in out


def test_format_existing_block_empty_when_no_open_tasks():
    assert "No open tasks" in dedup_mod.format_existing_block([])
    assert "No open tasks" in dedup_mod.format_existing_block(None)
    assert "No open tasks" in dedup_mod.format_existing_block([
        {"id": 9, "title": "done thing", "status": "done"},
    ])


def test_find_duplicate_match_flags_near_duplicates():
    existing = [
        {"id": 10, "title": "给 webui 加会话历史 sidebar", "status": "backlog"},
        {"id": 11, "title": "完全无关的任务", "status": "backlog"},
    ]
    hit = dedup_mod.find_duplicate_match("webui 加会话历史 sidebar", existing)
    assert hit is not None and hit["id"] == 10


def test_find_duplicate_match_no_false_positive():
    existing = [
        {"id": 10, "title": "webui 加 sidebar", "status": "backlog"},
    ]
    assert dedup_mod.find_duplicate_match("给 CLI 增加 --json 选项", existing) is None


def test_find_duplicate_match_ignores_closed_tasks():
    existing = [
        {"id": 10, "title": "webui 加 sidebar", "status": "done"},
    ]
    assert dedup_mod.find_duplicate_match("webui 加 sidebar", existing) is None


def test_filter_duplicate_tasks_splits_kept_and_dropped():
    existing = [
        {"id": 42, "title": "给 webui 加 sidebar", "status": "backlog"},
    ]
    planner_out = [
        {"title": "webui 加 sidebar", "priority": "P2"},
        {"title": "重构 db 模块", "priority": "P1"},
    ]
    kept, dropped = dedup_mod.filter_duplicate_tasks(planner_out, existing)
    assert len(kept) == 1
    assert kept[0]["title"] == "重构 db 模块"
    assert len(dropped) == 1
    assert dropped[0]["_dedup_matched_id"] == 42


def test_generate_task_breakdown_drops_duplicate_planner_output(tmp_path, monkeypatch):
    """End-to-end: planner suggests a dup -> generate_task_breakdown filters it out."""
    (tmp_path / "README.md").write_text("# x", encoding="utf-8")

    def fake_claude(prompt, schema, **kwargs):
        if "current_state" in schema.get("properties", {}):
            return {
                "current_state": "x", "relevant_files": [],
                "key_findings": [], "risks": [], "suggested_approach": "x",
            }
        return {
            "summary": "x",
            "complexity": "complex", "should_split": True,
            "tasks": [
                {
                    "title": "webui 加 sidebar",
                    "priority": "P2",
                    "goal": "x", "acceptance_criteria": ["a", "b"],
                    "builder_notes": [], "reviewer_notes": [],
                    "files": [], "notes": [], "depends_on_indices": [],
                },
                {
                    "title": "完全不同的新任务",
                    "priority": "P2",
                    "goal": "y", "acceptance_criteria": ["a", "b"],
                    "builder_notes": [], "reviewer_notes": [],
                    "files": [], "notes": [], "depends_on_indices": [],
                },
            ],
        }

    monkeypatch.setattr(ai_mod, "_run_claude_schema_prompt", fake_claude)

    result = ai_mod.generate_task_breakdown(
        "再给 webui 加个 sidebar",
        project_path=str(tmp_path),
        planner="claude",
        two_stage=True,
        existing_tasks=[
            {"id": 42, "title": "给 webui 加 sidebar", "status": "backlog"},
        ],
    )

    titles = [t["title"] for t in result["tasks"]]
    assert "webui 加 sidebar" not in titles
    assert "完全不同的新任务" in titles
    assert result.get("dedup_skipped")
    assert result["dedup_skipped"][0]["matched_existing_id"] == 42


def test_generate_task_breakdown_without_existing_tasks_skips_dedup(tmp_path, monkeypatch):
    """When caller passes no existing_tasks, nothing gets filtered on dedup grounds."""
    (tmp_path / "README.md").write_text("# x", encoding="utf-8")

    def fake_claude(prompt, schema, **kwargs):
        if "current_state" in schema.get("properties", {}):
            return {
                "current_state": "x", "relevant_files": [],
                "key_findings": [], "risks": [], "suggested_approach": "x",
            }
        return {
            "summary": "x", "complexity": "simple", "should_split": False,
            "tasks": [
                {
                    "title": "some task",
                    "priority": "P2",
                    "goal": "g", "acceptance_criteria": ["a", "b"],
                    "builder_notes": [], "reviewer_notes": [],
                    "files": [], "notes": [], "depends_on_indices": [],
                },
            ],
        }

    monkeypatch.setattr(ai_mod, "_run_claude_schema_prompt", fake_claude)

    result = ai_mod.generate_task_breakdown(
        "some task",
        project_path=str(tmp_path),
        planner="claude",
        two_stage=True,
    )
    assert len(result["tasks"]) == 1
    assert "dedup_skipped" not in result


def test_generate_task_breakdown_passes_existing_tasks_to_prompt(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# x", encoding="utf-8")
    prompt_seen = {}

    def fake_claude(prompt, schema, **kwargs):
        if "current_state" in schema.get("properties", {}):
            return {
                "current_state": "x", "relevant_files": [],
                "key_findings": [], "risks": [], "suggested_approach": "x",
            }
        prompt_seen["text"] = prompt
        return {
            "summary": "x", "complexity": "simple", "should_split": False,
            "tasks": [
                {
                    "title": "distinct new task",
                    "priority": "P2",
                    "goal": "g", "acceptance_criteria": ["a", "b"],
                    "builder_notes": [], "reviewer_notes": [],
                    "files": [], "notes": [], "depends_on_indices": [],
                },
            ],
        }

    monkeypatch.setattr(ai_mod, "_run_claude_schema_prompt", fake_claude)

    ai_mod.generate_task_breakdown(
        "distinct new task",
        project_path=str(tmp_path),
        planner="claude",
        two_stage=True,
        existing_tasks=[
            {"id": 7, "title": "已经在做的事", "status": "in_progress"},
        ],
    )

    text = prompt_seen["text"]
    assert "#7" in text
    assert "已经在做的事" in text


def test_parse_automation_planner_result_accepts_json_string():
    result = ai_mod.parse_automation_planner_result(
        """
        {
          "summary": "x",
          "tasks": [
            {
              "title": "do x",
              "priority": "P2",
              "goal": "g",
              "acceptance_criteria": ["a"],
              "builder_notes": [],
              "reviewer_notes": [],
              "files": [],
              "notes": []
            }
          ]
        }
        """,
        title="do x",
        max_tasks=3,
    )

    assert result["summary"] == "x"
    assert result["tasks"][0]["title"] == "do x"
    assert result["should_split"] is False


def test_parse_automation_planner_result_accepts_structured_output_envelope():
    result = ai_mod.parse_automation_planner_result(
        {
            "structured_output": {
                "summary": "wrapped",
                "tasks": [
                    {
                        "title": "do x",
                    }
                ],
            }
        },
        title="do x",
        max_tasks=3,
    )

    assert result["summary"] == "wrapped"
    assert result["tasks"][0]["title"] == "do x"


def test_parse_automation_planner_result_rejects_non_json_text():
    with pytest.raises(RuntimeError, match="不是有效 JSON"):
        ai_mod.parse_automation_planner_result(
            "not-json",
            title="do x",
            max_tasks=3,
        )


def test_parse_automation_planner_result_reports_placeholder_only_output():
    with pytest.raises(RuntimeError, match="占位任务"):
        ai_mod.parse_automation_planner_result(
            {
                "summary": "need more input",
                "tasks": [
                    {"title": "awaiting-user-input", "goal": "please provide details"},
                    {"title": "等待用户补充", "goal": "请提供上下文"},
                ],
            },
            title="优化一下",
            max_tasks=3,
        )


def test_parse_automation_planner_result_surfaces_dedup_to_progress_callback(monkeypatch):
    progress_messages: list[str] = []
    monkeypatch.setattr(ai_mod, "_planner_progress_callback", lambda line: progress_messages.append(line))

    result = ai_mod.parse_automation_planner_result(
        {
            "summary": "x",
            "tasks": [
                {
                    "title": "已有任务",
                    "priority": "P2",
                    "goal": "g",
                    "acceptance_criteria": ["a"],
                    "builder_notes": [],
                    "reviewer_notes": [],
                    "files": [],
                    "notes": [],
                }
            ],
        },
        title="已有任务",
        existing_tasks=[{"id": 42, "title": "已有任务", "status": "backlog"}],
    )

    assert result["tasks"] == []
    assert result.get("dedup_skipped")
    assert any("跳过重复任务" in line for line in progress_messages)


def test_parse_automation_planner_result_normalizes_task_fields_and_dependencies():
    result = ai_mod.parse_automation_planner_result(
        {
            "summary": 123,
            "complexity": "",
            "should_split": "yes",
            "tasks": [
                {
                    "title": "  first task  ",
                    "priority": "p9",
                    "goal": "",
                    "acceptance_criteria": "- 验收1\n- 验收2",
                    "builder_notes": "实现 first",
                    "reviewer_notes": [None, "  review first  "],
                    "files": "pkg/a.py\npkg/b.py",
                    "notes": 42,
                    "depends_on_indices": [0, -1, 99, "bad"],
                },
                {
                    "title": "second task",
                    "priority": "P1",
                    "goal": "完成 second",
                    "acceptance_criteria": ["验收A"],
                    "builder_notes": [],
                    "reviewer_notes": [],
                    "files": [],
                    "notes": [],
                    "depends_on_indices": [0, 1, 1],
                },
            ],
        },
        title="拆分任务",
        max_tasks=5,
    )

    assert result["summary"] == "123"
    assert result["complexity"] == "complex"
    assert result["should_split"] is True

    first, second = result["tasks"]
    assert first["title"] == "first task"
    assert first["priority"] == "P2"
    assert first["goal"].startswith("完成「first task」")
    assert first["acceptance_criteria"] == ["验收1", "验收2"]
    assert first["depends_on_indices"] == []

    assert second["priority"] == "P1"
    assert second["depends_on_indices"] == [0]


def test_generate_task_breakdown_can_return_raw_payload(tmp_path, monkeypatch):
    raw_payload = {
        "summary": "x",
        "tasks": [
            {
                "title": "do x",
                "priority": "P2",
                "goal": "g",
                "acceptance_criteria": ["a"],
                "builder_notes": [],
                "reviewer_notes": [],
                "files": [],
                "notes": [],
            }
        ],
    }

    monkeypatch.setattr(ai_mod, "_run_codex_schema_prompt", lambda *args, **kwargs: raw_payload)

    result = ai_mod.generate_task_breakdown(
        "do x",
        project_path=str(tmp_path),
        planner="codex",
        two_stage=False,
        parse_result=False,
    )

    assert result is raw_payload

