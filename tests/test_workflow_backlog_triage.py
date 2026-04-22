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
