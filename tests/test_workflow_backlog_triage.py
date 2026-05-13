from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.ai_support.agent_support import ai_guide_markdown, command_manifest
from codepilot.binary_support import manager as binary_mod
from codepilot.binary_support import paths as binary_paths_mod
from codepilot.storage import database as db
from codepilot.ai_support import service as ai_mod
from codepilot.core import progress_bus
from codepilot.gateway.service import GatewayResponse
from codepilot.core import runtime as runtime_mod
from codepilot.webapp import server as webui_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.core.config import load_project_config
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


@pytest.mark.slow
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
        ),
    )

    call_order: list[str] = []

    def fake_cleanup(*args, **kwargs):
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

    monkeypatch.setattr(run_cmd, "_cleanup_worktree_leftovers", fake_cleanup)
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
        ),
    )
    compliant_replan_content = (
        "# 补齐 reviewer 指出的输入校验缺口\n\n"
        "## Task Goal\n只修输入校验，不处理其它重构。\n\n"
        "## In Scope\n- 补齐 reviewer 指出的校验缺口\n\n"
        "## Out of Scope\n- 不重构 handler\n\n"
        "## Forbidden (Hard Boundary)\n- 不要新增依赖\n\n"
        "## Files In Scope\n- service/handler.py\n\n"
        "## Planning Evidence\n- reviewer 在上一轮明确点出 B1 / B2 校验缺失\n\n"
        "## Acceptance Criteria\n- [ ] reviewer 不再指出遗漏校验\n\n"
        "## Verification Matrix\n| AC | 命令 | 期望 | 证据 |\n| --- | --- | --- | --- |\n\n"
        "## Reviewer Checkpoints\n- 检查所有入参分支都有校验\n"
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
                "replan_content": compliant_replan_content,
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


def test_review_failure_fallback_uses_legacy_retry_path():
    from codepilot.commands import run_failure_triage as triage_mod

    captured: dict[str, object] = {}

    def fake_handle_failure(task, error_message, *, stop_on_failure=False):
        captured["task"] = task
        captured["error_message"] = error_message
        captured["stop_on_failure"] = stop_on_failure
        return {"id": task["id"], "status": "backlog"}, False

    result = triage_mod._fallback_review_failure_result(
        {"id": 7},
        "review failed",
        mark_task_failed_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not mark failed")),
        handle_failure_fn=fake_handle_failure,
        retry_on_failure=True,
        stop_on_failure=False,
    )

    assert result == {
        "updated": {"id": 7, "status": "backlog"},
        "error_message": "review failed",
        "decision": None,
        "should_stop": False,
    }
    assert captured["error_message"] == "review failed"
    assert captured["stop_on_failure"] is False


def test_override_review_failure_discard_decision_promotes_to_retry():
    from codepilot.commands import run_failure_triage as triage_mod
    from codepilot.commands.reviewer_output import ReviewerVerdict

    decision = {
        "action": "discard",
        "matched_task_id": None,
        "rationale": "",
        "merged_note": "drop it",
    }
    verdict = ReviewerVerdict(
        verdict="fail",
        blockers=["缺少 serializer.py"],
        advisory=[],
        ac_checks=[{"id": "AC3", "status": "fail", "reason": "导出列表不完整"}],
        source="json",
    )

    overridden = triage_mod._override_review_failure_discard_decision(
        decision,
        reviewer_verdict=verdict,
        review_output="VERDICT: FAIL",
        error_message="review 未通过",
    )

    assert overridden["action"] == "retry_with_hint"
    assert overridden["overridden_from"] == "discard"
    assert overridden["override_reason"] == "reviewer_actionable_failure"
    assert "serializer.py" in overridden["retry_hint"]


def test_apply_retry_with_hint_decision_marks_failed_when_retry_disabled():
    from codepilot.commands import run_failure_triage as triage_mod
    from codepilot.commands.reviewer_output import ReviewerVerdict

    task = {"id": 3, "content": "原始任务"}
    decision = {
        "action": "retry_with_hint",
        "rationale": "按 reviewer 修",
        "retry_hint": "补上缺失分支",
        "overridden_from": "discard",
    }
    verdict = ReviewerVerdict(verdict="fail", blockers=["补测试"], source="json")
    captured: dict[str, object] = {}

    def fake_mark_failed(task_arg, err):
        captured["error"] = err
        return {"id": task_arg["id"], "status": "failed", "error_message": err}

    result = triage_mod._apply_retry_with_hint_decision(
        task,
        "review failed",
        decision=decision,
        reviewer_verdict=verdict,
        mark_task_failed_fn=fake_mark_failed,
        handle_failure_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not retry")),
        db_module=None,
        retry_on_failure=False,
        stop_on_failure=True,
    )

    assert result["updated"]["status"] == "failed"
    assert result["should_stop"] is True
    assert "已禁用自动重试" in result["error_message"]
    assert "reviewer 有明确修复点" in captured["error"]


def test_apply_replan_decision_creates_followup_task():
    from codepilot.commands import run_failure_triage as triage_mod

    task = {
        "id": 12,
        "project": "demo",
        "title": "旧任务",
        "agent": "dual",
        "priority": "P1",
        "project_path": "/tmp/demo",
        "max_retries": 4,
    }
    decision = {
        "action": "replan",
        "rationale": "需要拆分",
        "replan_title": "新拆分任务",
        "replan_content": (
            "# 新拆分任务\n\n## Task Goal\n重做拆分。\n\n## In Scope\n- triage.py\n\n"
            "## Out of Scope\n- UI\n\n## Forbidden (Hard Boundary)\n- 不扩 scope\n\n"
            "## Files In Scope\n- triage.py\n\n## Planning Evidence\n- 来源 review\n\n"
            "## Acceptance Criteria\n- [ ] 可单测\n\n## Verification Matrix\n"
            "| AC | 命令 | 期望 | 证据 |\n| --- | --- | --- | --- |\n\n"
            "## Reviewer Checkpoints\n- 检查 helper 边界\n"
        ),
    }
    created: dict[str, object] = {}

    class FakeDB:
        def create_task(self, project, title, **kwargs):
            created["project"] = project
            created["title"] = title
            created["kwargs"] = kwargs
            return {"id": 99, "title": title}

    result = triage_mod._apply_replan_decision(
        task,
        "review failed",
        decision=decision,
        rationale="需要拆分",
        mark_task_failed_fn=lambda task_arg, err: {"id": task_arg["id"], "status": "failed", "error_message": err},
        db_module=FakeDB(),
        terminal_should_stop=True,
    )

    assert created["project"] == "demo"
    assert created["title"] == "新拆分任务"
    assert result["decision"]["created_task_id"] == 99
    assert "已转成新任务 #99" in result["error_message"]
    assert result["should_stop"] is True


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


def test_run_backlog_review_failure_replan_downgrades_when_content_violates_template(tmp_path, monkeypatch):
    """AI 给出的 replan_content 不满足 task-template 时必须降级为 discard。

    否则会把不合规的占位任务塞回 backlog，后续 add 校验起不到作用，与
    "每个任务都必须模板合规" 的全局不变量冲突。
    """
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
            review_output="范围太大\nVERDICT: FAIL",
            summary="review 未通过",
            executor="builtin",
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
                "rationale": "需要拆分但 AI 没给出完整模板",
                "merged_note": "",
                "retry_hint": "",
                "replan_title": "拆分任务",
                "replan_content": "## 任务目标\n\n仅几个要点，缺多个章节",
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
    assert followups == [], "non-compliant replan_content must NOT create a follow-up task"
    err = current["error_message"] or ""
    assert "已降级为 discard" in err
    assert "缺章节" in err


def test_retry_with_hint_appends_structured_reviewer_blockers_to_task_content(tmp_path, monkeypatch):
    """retry_with_hint 落地时必须把 reviewer blocker / failed AC 写进 task content。

    旧实现只 append decision["note"]（schema 里根本没有 note 字段，效果是
    永远 append 空串），下一轮 builder 看到的还是原始任务，等于对相同输入
    重跑。本测试守住"hint 块带 reviewer 结构化引用"这一关键不变量。
    """
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task(
        "demo",
        "处理校验缺口",
        content=(
            "# 处理校验缺口\n\n## Task Goal\n补齐入参校验。\n\n"
            "## In Scope\n- handler.py\n\n## Out of Scope\n- UI\n\n"
            "## Forbidden (Hard Boundary)\n- 不重构\n\n## Files In Scope\n- handler.py\n\n"
            "## Planning Evidence\n- 来自 spec\n\n## Acceptance Criteria\n- [ ] 全部分支可测\n\n"
            "## Verification Matrix\n| AC | 命令 | 期望 | 证据 |\n| --- | --- | --- | --- |\n\n"
            "## Reviewer Checkpoints\n- 检查所有入参\n"
        ),
        max_retries=3,
    )

    structured_review = (
        "需要修复的点\n"
        "- 见 JSON\n"
        "VERDICT: FAIL\n"
        "```json\n"
        '{"verdict":"fail",'
        '"blockers":["空字符串入参没拦","重复请求会重复落库"],'
        '"advisory":["命名建议改成 validate_payload"],'
        '"ac_checks":[{"id":"AC2","status":"fail","reason":"空 body 直接 200"}]}\n'
        "```"
    )

    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder failed",
            review_output=structured_review,
            summary="review 未通过",
            executor="builtin",
        ),
    )
    monkeypatch.setattr(
        run_cmd,
        "call_structured",
        lambda request: GatewayResponse(
            ok=True,
            source="cli:codex",
            payload={
                "action": "retry_with_hint",
                "matched_task_id": None,
                "rationale": "任务目标没问题，按 reviewer 反馈定向修",
                "merged_note": "",
                "retry_hint": "先补 B1 的空入参分支，再处理 B2 的幂等。",
                "replan_title": "",
                "replan_content": "",
            },
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    content = current["content"] or ""

    assert stats["requeued"] == 1
    assert current["status"] == "backlog"
    # AI 浓缩 hint 必须落地
    assert "AI triage 重试提示" in content
    assert "先补 B1 的空入参分支" in content
    # reviewer 结构化 blockers 必须以 B1/B2 的形式逐条带入
    assert "B1. 空字符串入参没拦" in content
    assert "B2. 重复请求会重复落库" in content
    # 失败的 AC 必须列出（含 reason）
    assert "AC2: 空 body 直接 200" in content
    # advisory 也要带（非阻塞但提示 builder）
    assert "A1. 命名建议改成 validate_payload" in content


def test_review_failure_discard_is_overridden_when_reviewer_has_actionable_blockers(tmp_path, monkeypatch):
    """reviewer 给出明确 blocker 时，AI 误判 discard 也必须自动转成重试。

    这覆盖任务 #33 暴露的问题：review 轮次耗尽后，反馈里已经写明缺哪些文件、
    哪些 AC 未过，这种失败不能被当成"丢弃单独跟进"后停住。
    """
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "建 serialization 子目录", content="原始任务", max_retries=3)

    structured_review = (
        "VERDICT: FAIL\n"
        "```json\n"
        '{"verdict":"fail",'
        '"blockers":["创建 deserialize.py / decoders.py / attachments.py，并更新 re-export"],'
        '"advisory":[],'
        '"ac_checks":[{"id":"AC5","status":"fail","reason":"serialization/*.py 只有 3 个，期望 4 个"}]}\n'
        "```"
    )
    monkeypatch.setattr(
        run_cmd,
        "_run_builtin_executor",
        lambda *args, **kwargs: run_cmd.ExecutionResult(
            exit_code=2,
            output="builder output",
            review_output=structured_review,
            summary="review 未通过（已用完重做轮次）",
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
                "rationale": "误判为无需跟进",
                "merged_note": "直接丢弃。",
                "retry_hint": "",
                "replan_title": "",
                "replan_content": "",
            },
        ),
    )

    stats = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    content = current["content"] or ""

    assert stats["requeued"] == 1
    assert stats["failed"] == 0
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1
    assert "覆盖 discard" in (current["error_message"] or "")
    assert "AI triage 重试提示" in content
    assert "deserialize.py / decoders.py / attachments.py" in content
    assert "AC5: serialization/*.py 只有 3 个" in content


def test_run_backlog_review_failure_falls_back_when_ai_triage_unavailable(tmp_path, monkeypatch):
    """AI gateway 不可用时（triage_fn 返回 None）必须保留 legacy retry 行为。

    没有这条 fallback，把 review-fail 路径接入 AI triage 后离线 / 限流场景
    就会变成"无 AI = 直接 fail"——用户在 codex review 里专门提醒过这一点，
    专项测试守住这条退化路径。
    """
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "no ai available", max_retries=2)

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
    # AI gateway 完全不可用：triage_fn 返回 None。
    monkeypatch.setattr(run_cmd, "_triage_review_failure", lambda *a, **kw: None)

    # 第 1 次：retry 预算还剩，应回退 backlog。
    first = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    assert first["requeued"] == 1
    assert first["failed"] == 0
    assert current["status"] == "backlog"
    assert current["retry_count"] == 1

    # 第 2 次：耗尽预算，应直接 mark failed，没有 AI triage 干预。
    second = run_cmd.run_backlog("demo", executor="builtin", auto_commit=False)
    current = db.get_task(task["id"])
    assert second["failed"] == 1
    assert current["status"] == "failed"
    assert current["retry_count"] == 2
    # 关键：error_message 不应包含 AI triage 标记，证明确实走了 legacy 路径。
    assert "AI triage" not in (current["error_message"] or "")


def test_replan_downgrade_respects_stop_on_failure_for_dispatch_mode(tmp_path, monkeypatch):
    """replan_content 不合规降级 discard 时，should_stop 必须复用 stop_on_failure。

    硬编码 True 会让 dispatch 模式（stop_on_failure=False）的"单任务失败"
    被错误放大成"整轮 run 停止"。codex 第二次 review 抓到的语义回归。
    """
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "needs replan but ai gives garbage", max_retries=3)

    captured: dict[str, object] = {}

    def fake_mark_failed(t, err):
        captured["mark_failed_called"] = True
        return db.update_task(t["id"], status="failed", error_message=err[:4000])

    def fake_handle_failure(*a, **kw):
        captured["handle_failure_called"] = True
        return db.get_task(task["id"]), True

    def fake_triage(task_arg, error_message, **kwargs):
        return {
            "action": "replan",
            "matched_task_id": None,
            "rationale": "AI 想拆分但内容残缺",
            "merged_note": "",
            "retry_hint": "",
            "replan_title": "",            # 缺标题 → 降级
            "replan_content": "## 任务目标\n仅一段散文",  # 缺章节 → 降级
        }

    from codepilot.commands.run_failure_triage import apply_review_failure_triage

    # dispatch 模式：stop_on_failure=False
    result_dispatch = apply_review_failure_triage(
        {"id": task["id"], "project": "demo", "title": "x", "content": "原始", "agent": "dual",
         "priority": "P2", "max_retries": 3},
        "review failed",
        review_output="VERDICT: FAIL",
        builder_output="",
        triage_fn=fake_triage,
        mark_task_failed_fn=fake_mark_failed,
        handle_failure_fn=fake_handle_failure,
        db_module=db,
        retry_on_failure=True,
        stop_on_failure=False,
    )

    assert result_dispatch["decision"]["downgraded_to"] == "discard"
    assert result_dispatch["should_stop"] is False, (
        "stop_on_failure=False 必须不被降级路径硬编码 True 覆盖"
    )

    # builtin 模式：stop_on_failure=True 仍然保留 True
    result_builtin = apply_review_failure_triage(
        {"id": task["id"], "project": "demo", "title": "x", "content": "原始", "agent": "dual",
         "priority": "P2", "max_retries": 3},
        "review failed",
        review_output="VERDICT: FAIL",
        builder_output="",
        triage_fn=fake_triage,
        mark_task_failed_fn=fake_mark_failed,
        handle_failure_fn=fake_handle_failure,
        db_module=db,
        retry_on_failure=True,
        stop_on_failure=True,
    )
    assert result_builtin["should_stop"] is True

