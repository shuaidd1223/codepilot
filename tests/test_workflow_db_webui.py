from __future__ import annotations

import json
import sqlite3
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
from codepilot.storage import session_store as db_session_store
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


def _valid_import_task_content(title: str) -> str:
    return f"""# {title}

## Task Goal

验证批量导入任务内容能被正确落库。

## In Scope

- 补齐导入动作测试

## Out of Scope

- 不修改执行器

## Forbidden (Hard Boundary)

- 不改数据库结构

## Files In Scope

- `codepilot/webapp/action_task_ops.py`

## Planning Evidence

来自批量导入动作回归测试。

## Acceptance Criteria

- [ ] 导入后任务正文完整

## Verification Matrix

| AC | Command | Expected | Evidence |
| :--- | :--- | :--- | :--- |
| AC1 | `pytest tests/test_workflow_db_webui.py` | 导入动作通过 | pytest |

## Reviewer Checkpoints

- 检查模板章节完整
"""


def test_init_db_infers_preversioned_schema_and_only_runs_missing_migrations(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'backlog',
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                last_output TEXT,
                stop_requested INTEGER NOT NULL DEFAULT 0,
                stop_reason TEXT,
                source TEXT NOT NULL DEFAULT 'user',
                dedup_key TEXT,
                fallback_reason TEXT
            );
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                content TEXT NOT NULL DEFAULT '',
                intent TEXT,
                task_ids TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

    db.init_db()
    status = db.schema_status()

    assert status["current_version"] == db.SCHEMA_VERSION
    assert [row["version"] for row in status["applied"]] == list(range(1, db.SCHEMA_VERSION + 1))
    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(session_messages)").fetchall()]
    assert "metadata" in columns


def test_init_db_does_not_record_missing_intermediate_preversioned_migration(tmp_path, monkeypatch):
    db_path = tmp_path / "partial-legacy.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'backlog',
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                last_output TEXT,
                stop_requested INTEGER NOT NULL DEFAULT 0,
                stop_reason TEXT,
                dedup_key TEXT,
                fallback_reason TEXT
            );
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                content TEXT NOT NULL DEFAULT '',
                intent TEXT,
                task_ids TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

    db.init_db()

    with sqlite3.connect(db_path) as conn:
        task_columns = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        message_columns = [row[1] for row in conn.execute("PRAGMA table_info(session_messages)").fetchall()]

    assert "source" in task_columns
    assert "metadata" in message_columns
    assert db.schema_status()["current_version"] == db.SCHEMA_VERSION


def test_insert_session_message_metadata_argument_is_optional(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    session = db.create_session("demo", title="chat")

    with db.get_write_conn() as conn:
        msg_id = db_session_store.insert_session_message(
            conn,
            session["id"],
            "assistant",
            "ok",
            "info",
            None,
        )
        row = db_session_store.fetch_session_message_by_id(conn, msg_id)

    assert row is not None
    assert row["metadata"] is None


def test_update_task_allows_core_fields(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "old title", agent="dual", priority="P2", depends_on=[1])

    updated = db.update_task(
        task["id"],
        title="new title",
        content="new content",
        agent="codex",
        priority="P0",
        depends_on=[2, 3],
    )

    assert updated["title"] == "new title"
    assert updated["content"] == "new content"
    assert updated["agent"] == "codex"
    assert updated["priority"] == "P0"
    assert json.loads(updated["depends_on"]) == [2, 3]


def test_increment_task_retry_eventually_fails(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me", max_retries=2)

    first = db.increment_task_retry(task["id"], "boom")
    second = db.increment_task_retry(task["id"], "boom again")

    assert first["status"] == "backlog"
    assert first["retry_count"] == 1
    assert second["status"] == "failed"
    assert second["retry_count"] == 2


def test_reset_task_for_retry_restores_backlog_state(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me", max_retries=2)
    db.update_task(
        task["id"],
        status="failed",
        retry_count=2,
        branch_name="feature/retry",
        worktree_path=str(project_path / "worktree"),
        error_message="boom",
        delivery_record="old delivery",
        started_at="2026-04-12T10:00:00",
        completed_at="2026-04-12T10:10:00",
        run_phase="reviewer",
        heartbeat_at="2026-04-12T10:05:00",
        active_pid=12345,
        current_log_path=str(project_path / "task.log"),
        last_output="broken output",
        stop_requested=1,
        stop_reason="stop",
    )

    reset = db.reset_task_for_retry(task["id"])

    assert reset["status"] == "backlog"
    assert reset["retry_count"] == 0
    assert reset["branch_name"] is None
    assert reset["worktree_path"] is None
    assert reset["error_message"] is None
    assert reset["delivery_record"] is None
    assert reset["started_at"] is None
    assert reset["completed_at"] is None
    assert reset["run_phase"] is None
    assert reset["heartbeat_at"] is None
    assert reset["active_pid"] is None
    assert reset["current_log_path"] is None
    assert reset["last_output"] is None
    assert reset["stop_requested"] == 0
    assert reset["stop_reason"] is None


def test_retry_command_requeues_failed_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me", max_retries=2)
    db.update_task(task["id"], status="failed", retry_count=2, error_message="boom")

    runner = CliRunner()
    result = runner.invoke(main, ["task", "retry", str(task["id"])])

    assert result.exit_code == 0
    current = db.get_task(task["id"])
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert current["error_message"] is None
    assert "已重新放回 backlog" in result.output
    assert "codepilot run -p demo" in result.output


def test_webui_dashboard_payload_lists_projects_and_tasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    first = db.create_task("demo", "task A", agent="codex", priority="P1")
    second = db.create_task("demo", "task B", agent="codex", priority="P2")
    db.update_task(second["id"], status="failed", error_message="boom")

    payload = webui_mod.dashboard_payload("demo")

    assert payload["selected_project"] == "demo"
    assert len(payload["projects"]) == 1
    assert payload["projects"][0]["stats"]["failed"] == 1
    assert [task["id"] for task in payload["tasks"]] == [first["id"], second["id"]]


def test_webui_retry_and_promote_actions_update_task_state(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "task A", agent="codex", priority="P2", max_retries=3)
    db.update_task(task["id"], status="failed", retry_count=2, error_message="boom")
    run_calls: list[dict] = []

    class _ImmediateThread:
        def __init__(self, *, target, name=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(
        "codepilot.webapp.actions.threading.Thread",
        _ImmediateThread,
    )
    monkeypatch.setattr(
        "codepilot.commands.run.run_backlog",
        lambda project, **kwargs: run_calls.append({"project": project, **kwargs}) or {"processed": 1},
    )

    retried = webui_mod.retry_task_action(task["id"])
    current = db.get_task(task["id"])
    assert retried["ok"] is True
    assert current["status"] == "backlog"
    assert current["retry_count"] == 0
    assert run_calls[0]["project"] == "demo"
    assert run_calls[0]["auto_commit"] is False

    promoted = webui_mod.promote_task_action(task["id"])
    current = db.get_task(task["id"])
    assert promoted["ok"] is True
    assert current["status"] == "backlog"
    assert current["priority"] == "P0"
    assert run_calls[1]["project"] == "demo"
    assert run_calls[1]["auto_commit"] is False


def test_webui_cancel_archive_delete_actions_follow_status_rules(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    backlog = db.create_task("demo", "待取消", agent="codex")
    cancelled = webui_mod.cancel_task_action(backlog["id"])
    assert cancelled["ok"] is True
    assert db.get_task(backlog["id"])["status"] == "cancelled"

    done = db.create_task("demo", "待归档", agent="codex")
    db.update_task(done["id"], status="done", completed_at="2026-04-23T10:00:00")
    archived = webui_mod.archive_task_action(done["id"])
    assert archived["ok"] is True
    assert db.get_task(done["id"])["status"] == "archived"

    payload = webui_mod.dashboard_payload("demo")
    assert done["id"] not in [task["id"] for task in payload["tasks"]]

    removed = webui_mod.delete_task_action(backlog["id"])
    assert removed["ok"] is True
    assert db.get_task(backlog["id"]) is None

    running = db.create_task("demo", "运行中", agent="codex")
    db.update_task(running["id"], status="in_progress")
    with pytest.raises(RuntimeError, match="不能删除"):
        webui_mod.delete_task_action(running["id"])


def test_webui_create_task_action_creates_task_for_project(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path), default_mode="codex")

    compliant_content = (
        "# 从 Web UI 新建任务\n\n"
        "## Task Goal\n演示通过 Web UI 创建任务。\n\n"
        "## In Scope\n- 写入数据库\n\n"
        "## Out of Scope\n- 不执行任务\n\n"
        "## Forbidden (Hard Boundary)\n- 不要污染其他项目\n\n"
        "## Files In Scope\n- N/A\n\n"
        "## Planning Evidence\n- 测试驱动\n\n"
        "## Acceptance Criteria\n- [ ] 任务出现在 backlog\n\n"
        "## Verification Matrix\n| AC | 命令 | 期望 | 证据 |\n| --- | --- | --- | --- |\n\n"
        "## Reviewer Checkpoints\n- 检查模板章节齐全\n"
    )
    created = webui_mod.create_task_action(
        "demo",
        "从 Web UI 新建任务",
        content=compliant_content,
        priority="P1",
        agent="auto",
        max_retries=4,
        mode="full",
    )

    assert created["ok"] is True
    assert created["mode"] == "full"
    task = db.get_task(created["task"]["id"])
    assert task["title"] == "从 Web UI 新建任务"
    # create_task_action strips trailing whitespace before saving.
    assert task["content"] == compliant_content.strip()
    assert task["priority"] == "P1"
    assert task["agent"] == "codex"
    assert task["max_retries"] == 4


def test_webui_task_detail_payload_contains_log_and_content(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "task A", content="详细任务内容", agent="codex", priority="P2")
    log_path = project_path / "task.log"
    log_path.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    db.update_task(
        task["id"],
        status="in_progress",
        current_log_path=str(log_path),
        error_message="",
        depends_on=[3, 4],
    )

    detail = webui_mod.task_detail_payload(task["id"])

    assert detail["content"] == "详细任务内容"
    assert detail["depends_on"] == [3, 4]
    assert "line-3" in detail["log_text"]
    assert detail["current_log_path"] == str(log_path)


def test_webui_task_detail_payload_tolerates_double_encoded_depends(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "task A", content="x", agent="codex", priority="P2")

    # Simulate historical bad rows where depends_on was double-encoded.
    db.update_task(task["id"], depends_on='"[]"')
    db.update_task(task["id"], depends_on='"[1, 2]"')

    detail = webui_mod.task_detail_payload(task["id"])

    assert detail["depends_on"] == [1, 2]


def test_webui_submit_requirement_action_records_job_and_tasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path), default_mode="codex")

    def fake_run_requirement_workflow(**kwargs):
        task = db.create_task(
            kwargs["project_info"]["name"],
            kwargs["title"],
            content="generated",
            agent=kwargs.get("task_agent") or "codex",
            priority=kwargs.get("priority") or "P2",
            project_path=kwargs["project_info"]["path"],
            max_retries=kwargs.get("max_retries") or 3,
        )
        return {
            "summary": "拆分完成",
            "tasks": [task],
            "run": {"done": 1, "failed": 0, "requeued": 0},
        }

    monkeypatch.setattr(webui_mod, "run_requirement_workflow", fake_run_requirement_workflow)
    monkeypatch.setattr(
        "codepilot.webapp.actions.clarify_requirement",
        lambda title, **kw: {"status": "ready", "refined_title": title},
    )

    result = webui_mod.submit_requirement_action(
        "demo",
        "让 Web UI 直接接收需求",
        execute=True,
        planner="codex",
        agent="codex",
        run_async=False,
    )

    assert result["ok"] is True
    jobs = webui_mod.list_ui_jobs("demo")
    assert jobs
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["task_ids"]
    task = db.get_task(jobs[0]["task_ids"][0])
    assert task["title"] == "让 Web UI 直接接收需求"


def test_retry_command_rejects_running_task(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))
    task = db.create_task("demo", "retry me")
    db.update_task(task["id"], status="in_progress", active_pid=12345)

    runner = CliRunner()
    result = runner.invoke(main, ["task", "retry", str(task["id"])])

    assert result.exit_code == 0
    current = db.get_task(task["id"])
    assert current["status"] == "in_progress"
    assert "正在运行中" in result.output


def test_auto_command_creates_linear_subtasks(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()

    db.register_project("demo", str(project_path))

    monkeypatch.setattr(
        auto_cmd,
        "generate_task_breakdown",
        lambda **kwargs: {
            "summary": "split ok",
            "tasks": [
                {
                    "title": "step 1",
                    "priority": "P1",
                    "goal": "do step 1",
                    "acceptance_criteria": ["a", "b"],
                    "builder_notes": ["code 1"],
                    "reviewer_notes": ["review 1"],
                    "files": ["a.py"],
                    "notes": ["note 1"],
                },
                {
                    "title": "step 2",
                    "priority": "P2",
                    "goal": "do step 2",
                    "acceptance_criteria": ["c", "d"],
                    "builder_notes": ["code 2"],
                    "reviewer_notes": ["review 2"],
                    "files": ["b.py"],
                    "notes": ["note 2"],
                },
            ],
        },
    )

    runner = CliRunner()
    result = runner.invoke(main, ["auto", "-p", "demo", "-t", "big goal", "--plan-only"])

    assert result.exit_code == 0
    tasks = sorted(db.list_tasks(project="demo"), key=lambda item: item["id"])
    assert len(tasks) == 2
    assert tasks[0]["title"] == "step 1"
    assert tasks[0]["depends_on"] is None
    assert tasks[1]["title"] == "step 2"
    assert json.loads(tasks[1]["depends_on"]) == [tasks[0]["id"]]


def test_webui_dashboard_task_sort_matches_display_rules(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    backlog_p1 = db.create_task("demo", "backlog-p1", priority="P1")
    backlog_p0 = db.create_task("demo", "backlog-p0", priority="P0")
    done_early = db.create_task("demo", "done-early", priority="P0")
    done_late = db.create_task("demo", "done-late", priority="P2")
    db.update_task(done_early["id"], status="done", completed_at="2026-04-23T10:00:00")
    db.update_task(done_late["id"], status="done", completed_at="2026-04-23T11:00:00")

    with db.get_write_conn() as conn:
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ("2026-04-23T08:00:00", backlog_p1["id"]),
        )
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ("2026-04-23T09:00:00", backlog_p0["id"]),
        )
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ("2026-04-23T07:00:00", done_early["id"]),
        )
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ("2026-04-23T06:00:00", done_late["id"]),
        )

    payload = webui_mod.dashboard_payload("demo")
    ordered_ids = [item["id"] for item in payload["tasks"]]
    assert ordered_ids == [backlog_p0["id"], backlog_p1["id"], done_late["id"], done_early["id"]]


def test_import_tasks_action_normalizes_legacy_content_and_depends_fields(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    upstream = db.create_task("demo", "upstream", content=_valid_import_task_content("upstream"), agent="codex")

    result = webui_mod.import_tasks_action(
        "demo",
        [
            {
                "name": "import task 1",
                "description": _valid_import_task_content("import task 1"),
                "depends": [upstream["id"], upstream["id"], 0, "bad"],
            },
            {
                "title": "import task 2",
                "body": _valid_import_task_content("import task 2"),
                "dependsOn": f"{upstream['id']}, {upstream['id']}, 0, bad",
            },
        ],
    )

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["tasks"][0]["title"] == "import task 1"
    assert result["tasks"][1]["title"] == "import task 2"

    imported_first = db.get_task(result["tasks"][0]["id"])
    imported_second = db.get_task(result["tasks"][1]["id"])
    assert json.loads(imported_first["depends_on"]) == [upstream["id"]]
    assert json.loads(imported_second["depends_on"]) == [upstream["id"]]


def test_webui_requirement_list_sort_matches_display_rules(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    with webui_mod._UI_LOCK:
        webui_mod._UI_JOBS = {
            1: {
                "id": 1,
                "project": "demo",
                "title": "queued-p1",
                "status": "queued",
                "phase": "queued",
                "priority": "P1",
                "created_at": "2026-04-23T10:00:00",
                "updated_at": "2026-04-23T10:00:00",
                "finished_at": "",
            },
            2: {
                "id": 2,
                "project": "demo",
                "title": "running-p0",
                "status": "running",
                "phase": "planning",
                "priority": "P0",
                "created_at": "2026-04-23T12:00:00",
                "updated_at": "2026-04-23T12:00:00",
                "finished_at": "",
            },
            3: {
                "id": 3,
                "project": "demo",
                "title": "done-older",
                "status": "succeeded",
                "phase": "done",
                "priority": "P0",
                "created_at": "2026-04-23T09:00:00",
                "updated_at": "2026-04-23T11:00:00",
                "finished_at": "2026-04-23T11:00:00",
            },
            4: {
                "id": 4,
                "project": "demo",
                "title": "done-newer",
                "status": "succeeded",
                "phase": "done",
                "priority": "P2",
                "created_at": "2026-04-23T08:00:00",
                "updated_at": "2026-04-23T13:00:00",
                "finished_at": "2026-04-23T13:00:00",
            },
        }

    jobs = webui_mod.list_ui_jobs("demo")
    assert [job["id"] for job in jobs] == [2, 1, 4, 3]


def test_webui_session_list_sorts_by_activity_then_creation(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    first = db.create_session("demo", title="first")
    second = db.create_session("demo", title="second")
    third = db.create_session("demo", title="third")

    with db.get_write_conn() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ?, created_at = ? WHERE id = ?",
            ("2026-04-23T10:00:00", "2026-04-23T09:00:00", first["id"]),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ?, created_at = ? WHERE id = ?",
            ("2026-04-23T10:00:00", "2026-04-23T09:30:00", second["id"]),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ?, created_at = ? WHERE id = ?",
            ("2026-04-23T10:30:00", "2026-04-23T08:30:00", third["id"]),
        )

    payload = webui_mod.list_sessions_action("demo")
    assert [item["id"] for item in payload["sessions"]] == [third["id"], second["id"], first["id"]]

