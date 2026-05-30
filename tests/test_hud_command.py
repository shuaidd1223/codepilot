from __future__ import annotations

import json

from click.testing import CliRunner

from codepilot.cli import main
from codepilot.commands.hud import collect_hud_snapshot
from codepilot.storage import database as db
from tests.workflow_testkit import init_test_db


def _seed_project(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.register_project("demo", str(project_path), default_mode="dual")

    running = db.create_task(
        "demo",
        "运行中的任务",
        "content-a",
        agent="codex",
        priority="P1",
    )
    db.update_task(
        running["id"],
        status="in_progress",
        run_phase="builder",
        heartbeat_at="2026-04-29 12:00:00",
        last_output="正在执行 builder 阶段",
    )

    failed = db.create_task(
        "demo",
        "失败任务",
        "content-b",
        agent="dual",
        priority="P0",
    )
    db.update_task(
        failed["id"],
        status="failed",
        error_message="测试失败",
        completed_at="2026-04-29 12:05:00",
    )

    db.create_task(
        "demo",
        "待办任务",
        "content-c",
        agent="dual",
        priority="P2",
    )
    db.upsert_service_state(
        "webui",
        "demo",
        pid=123,
        status="running",
        heartbeat_at="2026-04-29 12:06:00",
        log_path=str(tmp_path / "webui.log"),
    )


def test_collect_hud_snapshot_summarizes_project_and_services(tmp_path, monkeypatch):
    _seed_project(tmp_path, monkeypatch)

    snapshot = collect_hud_snapshot(project="demo", preset="full")

    assert snapshot["project"] == "demo"
    assert snapshot["totals"]["in_progress"] == 1
    assert snapshot["totals"]["failed"] == 1
    assert snapshot["totals"]["backlog"] == 1
    assert snapshot["projects"][0]["mode"] == "attention"
    assert snapshot["projects"][0]["active_tasks"][0]["phase"] == "builder"
    assert snapshot["services"][0]["label"] == "webui:demo"


def test_hud_snapshot_shows_dirty_worktree_blocked_reason(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    task = db.create_task("demo", "blocked task", agent="dual")
    db.update_task(
        task["id"],
        status="backlog",
        error_message=(
            "内置执行器检测到主工作区已有未提交改动。"
            "当前预检策略为 stop，本次跳过执行且不消耗重试次数。"
        ),
    )

    snapshot = collect_hud_snapshot(project="demo", preset="full")

    assert snapshot["projects"][0].get("blocked_reason") is not None, (
        "HUD project snapshot should include blocked_reason when "
        "a backlog task has a dirty-worktree error"
    )


def test_hud_command_emits_json_payload(tmp_path, monkeypatch):
    _seed_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["hud", "-p", "demo", "--preset", "full", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "hud"
    data = payload["data"]
    assert data["project"] == "demo"
    assert data["projects"][0]["stats"]["failed"] == 1
    assert data["services"][0]["status"] == "running"
