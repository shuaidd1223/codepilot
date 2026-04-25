from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.agent_support import ai_guide_markdown, command_manifest
from codepilot import binary as binary_mod
from codepilot import binary_paths as binary_paths_mod
from codepilot import db
from codepilot import ai as ai_mod
from codepilot import progress_bus
from codepilot.ai_gateway import GatewayResponse
from codepilot import runtime as runtime_mod
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.config import load_project_config
from tests.workflow_testkit import init_test_db as _init_test_db


def test_run_backlog_builtin_stops_after_retry_limit(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", max_retries=2)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(exit_code=1, output="builder failed", executor="builtin"),
    )
    # Disable AI review triage so the test stays focused on the legacy retry
    # budget logic. apply_review_failure_triage falls back to handle_failure
    # when triage_fn returns None.
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *a, **kw: None)

    first = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    assert first["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1

    second = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    assert second["failed"] == 1
    assert current["status"] == "failed"
    assert current["retry_count"] == 2


def test_run_backlog_can_fail_without_requeue(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", max_retries=3)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder failed",
            review_output="VERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False, retry_on_failure=False)
    current = db.get_task(task["id"])

    assert stats["failed"] == 1
    assert stats["requeued"] == 0
    assert current["status"] == "failed"
    assert current["retry_count"] == 1
    assert "review 未通过" in (current["error_message"] or "")


def test_run_backlog_deterministic_failure_merges_triage_note_into_existing_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", max_retries=3)
    target = db.create_task("demo", "统一修 reviewer 失败", content="已有 backlog 内容")

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder failed",
            review_output="VERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
            deterministic_failure=True,
        ),
    )
    monkeypatch.setattr(
        run_cmd,
        "call_structured",
        lambda request: GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "merge",
                "matched_task_id": target["id"],
                "rationale": "已有 backlog 任务覆盖这类 reviewer 修复",
                "merged_note": "补充这次 reviewer 失败的上下文，统一处理。",
            },
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    merged_target = db.get_task(target["id"])

    assert stats["failed"] == 1
    assert stats["requeued"] == 0
    assert current["status"] == "failed"
    assert current["retry_count"] == 1
    assert f"AI triage: 已归并到 #{target['id']}" in (current["error_message"] or "")
    assert "AI triage 归并记录" in (merged_target["content"] or "")
    assert f"来源任务: #{task['id']} {task['title']}" in (merged_target["content"] or "")


def test_run_backlog_deterministic_failure_can_discard_triage_followup(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", max_retries=3)
    target = db.create_task("demo", "现有 backlog", content="保持不变")

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder failed",
            review_output="VERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
            deterministic_failure=True,
        ),
    )
    monkeypatch.setattr(
        run_cmd,
        "call_structured",
        lambda request: GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "discard",
                "matched_task_id": None,
                "rationale": "失败信息没有形成独立 backlog 价值",
                "merged_note": "直接丢弃，不再派生跟进项。",
            },
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    untouched_target = db.get_task(target["id"])

    assert stats["failed"] == 1
    assert stats["requeued"] == 0
    assert current["status"] == "failed"
    assert current["retry_count"] == 1
    assert "AI triage: 已丢弃单独跟进" in (current["error_message"] or "")
    assert untouched_target["content"] == "保持不变"


def test_run_backlog_review_failure_retry_with_hint_requeues_and_appends_hint(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", content="原始任务内容", max_retries=3)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder failed",
            review_output="请先修复遗漏校验\nVERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
            deterministic_failure=True,
        ),
    )

    call_order: list[str] = []

    def fake_cleanup(**kwargs):
        call_order.append("cleanup")

    def fake_call_structured(request):
        assert call_order == ["cleanup"]
        return GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "retry_with_hint",
                "matched_task_id": None,
                "rationale": "任务目标没问题，但需要把 reviewer 的阻塞点明确灌回去",
                "merged_note": "",
                "retry_hint": "先补上遗漏校验，再重新跑 review，不要改其它文件。",
                "replan_title": "",
                "replan_content": "",
            },
        )

    monkeypatch.setattr(run_cmd, "_finalize_failed_task_workspace", fake_cleanup)
    monkeypatch.setattr(run_cmd, "call_structured", fake_call_structured)

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])

    assert stats["failed"] == 0
    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1
    assert "AI triage: 已追加重试提示并回退 backlog" in (current["error_message"] or "")
    assert "AI triage 重试提示" in (current["content"] or "")
    assert "先补上遗漏校验" in (current["content"] or "")


def test_run_backlog_review_failure_replans_into_new_backlog_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", content="原始任务内容", max_retries=3)

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder failed",
            review_output="当前任务范围太大\nVERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
            deterministic_failure=True,
        ),
    )
    monkeypatch.setattr(
        run_cmd,
        "call_structured",
        lambda request: GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "replan",
                "matched_task_id": None,
                "rationale": "当前任务范围过大，应该拆成更聚焦的后续任务",
                "merged_note": "",
                "retry_hint": "",
                "replan_title": "补齐 reviewer 指出的输入校验缺口",
                "replan_content": "## 任务目标\n\n只修输入校验，不处理其它重构。\n\n## 验收标准\n\n- reviewer 不再指出遗漏校验",
            },
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    tasks = db.list_tasks(project="demo")
    followups = [item for item in tasks if item["id"] != task["id"]]

    assert stats["failed"] == 1
    assert stats["requeued"] == 0
    assert current["status"] == "failed"
    assert current["retry_count"] == 1
    assert len(followups) == 1
    assert followups[0]["status"] == "backlog"
    assert followups[0]["title"] == "补齐 reviewer 指出的输入校验缺口"
    assert "AI triage 来源" in (followups[0]["content"] or "")
    assert f"AI triage: 已转成新任务 #{followups[0]['id']}" in (current["error_message"] or "")


def test_run_backlog_review_failure_can_merge_partial_into_existing_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task", max_retries=3)
    target = db.create_task("demo", "统一处理 reviewer 遗留项", content="已有 backlog 内容")

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder failed",
            review_output="VERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
            deterministic_failure=True,
        ),
    )
    monkeypatch.setattr(
        run_cmd,
        "call_structured",
        lambda request: GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "merge_partial",
                "matched_task_id": target["id"],
                "rationale": "已有 backlog 任务覆盖这类 reviewer 修复",
                "merged_note": "补充这次 reviewer 失败的上下文，统一处理。",
                "retry_hint": "",
                "replan_title": "",
                "replan_content": "",
            },
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    merged_target = db.get_task(target["id"])

    assert stats["failed"] == 1
    assert stats["requeued"] == 0
    assert current["status"] == "failed"
    assert current["retry_count"] == 1
    assert f"AI triage: 已归并到 #{target['id']}" in (current["error_message"] or "")
    assert "AI triage 归并记录" in (merged_target["content"] or "")
    assert f"来源任务: #{task['id']} {task['title']}" in (merged_target["content"] or "")


def test_apply_deterministic_failure_triage_rejects_merge_to_unsurfaced_candidate(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task")
    hidden_target = db.create_task("demo", "较早的 backlog", content="保持原样", priority="P9")
    for idx in range(run_cmd._DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT):
        db.create_task("demo", f"filler {idx}", priority="P0")

    monkeypatch.setattr(
        run_cmd,
        "call_structured",
        lambda request: GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "merge",
                "matched_task_id": hidden_target["id"],
                "rationale": "这个旧任务也算覆盖",
                "merged_note": "错误地归并到未展示候选。",
            },
        ),
    )

    error_message = run_cmd._apply_deterministic_failure_triage(task, "review 未通过")
    current_target = db.get_task(hidden_target["id"])

    assert error_message == "review 未通过"
    assert current_target["content"] == "保持原样"


def test_apply_deterministic_failure_triage_rejects_invalid_discard_payload(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task")
    target = db.create_task("demo", "现有 backlog", content="保持不变")

    monkeypatch.setattr(
        run_cmd,
        "call_structured",
        lambda request: GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "discard",
                "matched_task_id": target["id"],
                "rationale": "虽然写了 discard，但还带了候选 id",
                "merged_note": "这份响应不合法。",
            },
        ),
    )

    error_message = run_cmd._apply_deterministic_failure_triage(task, "review 未通过")

    assert error_message == "review 未通过"


def test_triage_deterministic_failure_falls_back_to_registered_project_path(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "broken task")
    db.create_task("demo", "现有 backlog")
    db.update_task(task["id"], project_path="")
    task = db.get_task(task["id"])

    seen: dict[str, str] = {}

    def fake_call_structured(request):
        seen["project_path"] = request.project_path
        return GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "discard",
                "matched_task_id": None,
                "rationale": "无需额外待办",
                "merged_note": "直接丢弃。",
            },
        )

    monkeypatch.setattr(run_cmd, "call_structured", fake_call_structured)

    decision = run_cmd._triage_deterministic_failure(task, "review 未通过")

    assert decision is not None
    assert seen["project_path"] == str(project_path)


def test_triage_deterministic_failure_uses_registered_config_file(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    config_root = tmp_path / "config-root"
    project_path.mkdir()
    config_root.mkdir()
    config_file = config_root / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
default_mode = "codex"
""".strip(),
        encoding="utf-8",
    )

    db.register_project("demo", str(project_path), config_file=str(config_file))
    task = db.create_task("demo", "broken task")
    db.create_task("demo", "现有 backlog")

    seen: dict[str, str] = {}

    def fake_call_structured(request):
        seen["project_path"] = request.project_path
        seen["config_ref"] = request.config_ref
        return GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "discard",
                "matched_task_id": None,
                "rationale": "无需额外待办",
                "merged_note": "直接丢弃。",
            },
        )

    monkeypatch.setattr(run_cmd, "call_structured", fake_call_structured)

    decision = run_cmd._triage_deterministic_failure(task, "review 未通过")

    assert decision is not None
    assert seen["project_path"] == str(project_path)
    assert seen["config_ref"] == str(config_file)


def test_triage_deterministic_failure_skips_ai_without_resolved_project_path(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    db.register_project("demo", str(tmp_path / "project"))
    task = db.create_task("demo", "broken task")
    db.create_task("demo", "现有 backlog")
    db.update_task(task["id"], project_path="")
    task = db.get_task(task["id"])

    called = {"value": False}

    def fake_call_structured(request):
        called["value"] = True
        return GatewayResponse(ok=False, source="default", error="should not run")

    monkeypatch.setattr(run_cmd.db, "get_project", lambda name: {"name": name, "path": "", "config_file": ""})
    monkeypatch.setattr(run_cmd, "call_structured", fake_call_structured)

    decision = run_cmd._triage_deterministic_failure(task, "review 未通过")

    assert decision is None
    assert called["value"] is False


def test_review_failure_prompt_injects_structured_reviewer_blockers():
    """Triage prompt must surface parsed blockers + grounded-citation rule.

    Without this, replan_content drifts to generic recommendations because
    the AI only sees the trimmed reviewer prose; with structured blockers
    in the prompt the AI can (and is required to) cite specific items.
    """
    from codepilot.commands.run_failure_triage import _build_review_failure_triage_prompt
    from codepilot.commands.reviewer_output import ReviewerVerdict

    verdict = ReviewerVerdict(
        verdict="fail",
        blockers=[
            "缺少对空字符串输入的校验",
            "异常路径里没有写日志",
            "重复请求会重复落库",
        ],
        advisory=["函数命名建议改成 validate_payload"],
        ac_checks=[
            {"id": "AC2", "status": "fail", "reason": "空 body 直接 200"},
            {"id": "AC1", "status": "pass", "reason": ""},
        ],
        source="json",
    )
    prompt = _build_review_failure_triage_prompt(
        {"id": 7, "title": "校验输入", "priority": "P1", "content": "原始任务"},
        error_message="reviewer FAIL",
        review_output="...",
        builder_output="...",
        candidates=[],
        reviewer_verdict=verdict,
    )

    assert "reviewer 结构化反馈" in prompt
    assert "B1. 缺少对空字符串输入的校验" in prompt
    assert "B2. 异常路径里没有写日志" in prompt
    assert "B3. 重复请求会重复落库" in prompt
    assert "AC2" in prompt and "空 body 直接 200" in prompt
    # Passing AC must NOT show up in the failed-AC block.
    assert "AC1" not in prompt.split("ac_checks(status=fail):", 1)[1].split("提醒项")[0] \
        if "ac_checks(status=fail):" in prompt else True
    assert "A1. 函数命名建议改成 validate_payload" in prompt
    assert "明确引用" in prompt and "B1/B2" in prompt


def test_review_failure_prompt_omits_block_when_verdict_empty():
    """Empty / unparseable verdict must keep the legacy prompt shape.

    Reviewers that haven't adopted the JSON fence still emit free-form
    text; the triage flow must not regress for them.
    """
    from codepilot.commands.run_failure_triage import _build_review_failure_triage_prompt
    from codepilot.commands.reviewer_output import ReviewerVerdict

    verdict = ReviewerVerdict(verdict="unknown", source="empty")
    prompt = _build_review_failure_triage_prompt(
        {"id": 7, "title": "demo", "priority": "P2", "content": ""},
        error_message="boom",
        review_output="some prose without json fence",
        builder_output="",
        candidates=[],
        reviewer_verdict=verdict,
    )

    assert "reviewer 结构化反馈" not in prompt
    assert "B1." not in prompt
    # The grounded-citation rule is only added when blockers exist.
    assert "明确引用" not in prompt


def test_review_failure_evidence_parses_review_output_for_prompt(tmp_path, monkeypatch):
    """End-to-end: _collect_review_failure_evidence must parse review_output
    and feed structured blockers into the prompt builder."""
    from codepilot.commands import run_failure_triage as triage_mod

    fake_config = types.SimpleNamespace(
        classifier=types.SimpleNamespace(provider="codex", model="gpt-x", timeout=10),
        providers={"codex": types.SimpleNamespace(base_url=None)},
        get_provider_api_key=lambda key: "fake-key",
    )
    captured: dict[str, object] = {}

    def fake_build(task, **kwargs):
        captured["reviewer_verdict"] = kwargs.get("reviewer_verdict")
        return "<<prompt>>"

    monkeypatch.setattr(triage_mod, "_build_review_failure_triage_prompt", fake_build)

    review_output = (
        "需要修复的点：\n- 缺少 200 行回归用例\n- HEAD 校验未覆盖\nVERDICT: FAIL"
    )
    project_path = tmp_path / "proj"
    project_path.mkdir()

    class FakeDB:
        def list_tasks(self, project=None):
            return []
        def get_task(self, task_id):
            return None
        def get_project(self, name):
            return {"name": name, "path": str(project_path), "config_file": ""}

    evidence = triage_mod._collect_review_failure_evidence(
        {"id": 9, "project": "demo", "title": "x", "content": "", "priority": "P2", "path": str(project_path)},
        error_message="boom",
        review_output=review_output,
        builder_output="builder",
        db_module=FakeDB(),
        resolve_project_config_reference_fn=lambda task: str(project_path),
        load_project_config_fn=lambda task: fake_config,
        resolve_planner_fn=lambda config, kind: "codex",
        candidate_limit=10,
    )

    assert evidence is not None
    verdict = captured.get("reviewer_verdict")
    assert verdict is not None
    assert verdict.verdict == "fail"
    assert verdict.source in {"legacy", "json"}
    # The legacy parser collapses bullets into a single multiline blocker
    # entry; what matters is at least one blocker reaches the prompt.
    assert any("回归" in str(item) or "HEAD" in str(item) for item in verdict.blockers)
