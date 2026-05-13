from __future__ import annotations

import ast
import inspect

from codepilot.ai_support import question_runtime as runtime
from codepilot.storage import database as db
from tests.chat_flow_testkit import register_project


def _branch_count(function) -> int:
    tree = ast.parse(inspect.getsource(function))
    branch_nodes = (
        ast.BoolOp,
        ast.ExceptHandler,
        ast.For,
        ast.If,
        ast.IfExp,
        ast.Match,
        ast.Try,
        ast.While,
        ast.comprehension,
    )
    return sum(isinstance(node, branch_nodes) for node in ast.walk(tree))


def test_heuristic_runtime_plan_keeps_lookup_order_and_scope():
    plan = runtime._heuristic_question_runtime_plan("所有项目的任务进度、失败任务和服务状态怎么样")

    assert plan["use_local_data"] is True
    assert plan["project_scope"] == "all"
    assert [lookup["tool"] for lookup in plan["lookups"]] == [
        "service_status",
        "task_stats",
        "failed_tasks",
    ]


def test_runtime_lookup_execution_collects_task_and_service_snapshots(tmp_path, monkeypatch):
    project_path = register_project(tmp_path, monkeypatch)
    running = db.create_task("demo", "running task", priority="P1")
    failed = db.create_task("demo", "failed task", priority="P0")
    done = db.create_task("demo", "done task")
    db.update_task(running["id"], status="in_progress")
    db.update_task(failed["id"], status="failed", error_message="boom")
    db.update_task(done["id"], status="done")
    db.upsert_service_state("daemon", "demo", pid=1234, status="running")
    db.upsert_service_state("inspect", "demo", pid=0, status="stopped")

    payload = runtime._execute_question_runtime_lookups(
        {
            "project_scope": "current",
            "lookups": [
                {"tool": "task_stats"},
                {"tool": "running_tasks", "limit": 2},
                {"tool": "failed_tasks", "limit": 2},
                {"tool": "service_status"},
            ],
        },
        project_path=str(project_path),
    )

    assert payload["current_project"]["name"] == "demo"
    lookups = {entry["tool"]: entry["items"] for entry in payload["lookups"]}
    assert lookups["task_stats"][0]["stats"]["total"] == 3
    assert lookups["running_tasks"][0]["items"] == [
        {"id": running["id"], "title": "running task", "priority": "P1"}
    ]
    assert lookups["failed_tasks"][0]["items"] == [
        {"id": failed["id"], "title": "failed task", "priority": "P0", "error_message": "boom"}
    ]
    assert lookups["service_status"][0]["services"]["daemon"]["running"] is True
    assert lookups["service_status"][0]["services"]["inspect"]["running"] is False


def test_question_runtime_hotspot_functions_stay_small():
    assert _branch_count(runtime._heuristic_question_runtime_plan) <= 10
    assert _branch_count(runtime._execute_runtime_lookup_request) <= 4
    assert _branch_count(runtime._render_runtime_lookup_entry) <= 2
