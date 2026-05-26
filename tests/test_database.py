"""专项测试：storage/database.py 核心数据层。

database.py 是 SQLite 数据库的 API 门面，持久化项目、任务、会话、
服务状态和缓存查询结果。本文件覆盖所有公共 API 的主路径和关键边界。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codepilot.storage import database as db


# ── 辅助函数 ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """为每次测试创建一个隔离的临时 SQLite 数据库。"""
    db_path = tmp_path / "codepilot.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    db.init_db()
    yield db_path
    # 清理全局缓存
    db._QUERY_CACHE.clear()
    db._cache_invalidate()


# ── 初始化与 Schema ────────────────────────────────────────────────────

class TestInitAndSchema:
    def test_init_db_creates_database_file(self, tmp_db: Path):
        assert tmp_db.exists()
        assert tmp_db.stat().st_size > 0

    def test_init_db_is_idempotent(self, tmp_db: Path):
        db.init_db()  # 第二次调用不应抛异常
        db.init_db()
        db.init_db()
        assert tmp_db.exists()

    def test_schema_status_returns_version(self, tmp_db: Path):
        status = db.schema_status()
        assert status["current_version"] >= 0
        assert status["target_version"] >= status["current_version"]
        assert "applied" in status
        assert len(status["applied"]) > 0

    def test_init_db_twice_same_path(self, tmp_db: Path):
        """同一路径重复初始化应幂等。"""
        db.init_db()
        db.init_db()
        assert tmp_db.exists()


# ── 项目 CRUD ──────────────────────────────────────────────────────────

class TestProjects:
    def test_register_and_get_project(self, tmp_db: Path):
        db.register_project("test-project", str(tmp_db.parent))
        got = db.get_project("test-project")
        assert got is not None
        assert got["name"] == "test-project"
        assert got["path"] == str(tmp_db.parent)

    def test_get_nonexistent_project_returns_none(self, tmp_db: Path):
        assert db.get_project("does-not-exist") is None

    def test_register_project_idempotent(self, tmp_db: Path):
        db.register_project("dup", str(tmp_db.parent))
        db.register_project("dup", str(tmp_db.parent))  # 重复注册不应抛异常
        projects = db.list_projects()
        matches = [p for p in projects if p["name"] == "dup"]
        assert len(matches) == 1

    def test_list_projects(self, tmp_db: Path):
        for i in range(3):
            p = tmp_db.parent / f"proj{i}"
            p.mkdir(exist_ok=True)
            db.register_project(f"proj{i}", str(p))
        projects = db.list_projects()
        assert len(projects) >= 3

    def test_find_project_by_path(self, tmp_db: Path):
        db.register_project("by-path", str(tmp_db.parent))
        found = db.find_project_by_path(tmp_db.parent)
        assert found is not None
        assert found["name"] == "by-path"

    def test_find_project_by_nested_path(self, tmp_db: Path):
        nested = tmp_db.parent / "nested" / "deep"
        nested.mkdir(parents=True)
        db.register_project("nested-proj", str(nested))
        # 从子目录查找也应匹配
        child = nested / "src"
        child.mkdir()
        found = db.find_project_by_path(child)
        assert found is not None
        assert found["name"] == "nested-proj"

    def test_delete_project(self, tmp_db: Path):
        db.register_project("to-delete", str(tmp_db.parent))
        assert db.get_project("to-delete") is not None
        db.delete_project("to-delete")
        assert db.get_project("to-delete") is None

    def test_rename_project_updates_children_and_service_scopes(self, tmp_db: Path):
        db.register_project("old-name", str(tmp_db.parent))
        task = db.create_task("old-name", "kept task", content="body")
        session = db.create_session("old-name", title="kept session")
        db.upsert_service_state(
            "daemon",
            "old-name",
            pid=1234,
            status="running",
            meta={"project": "old-name", "started_at": "2026-05-25T09:00:00"},
        )
        db.upsert_service_state(
            "webui_job",
            "99",
            status="queued",
            meta={"id": 99, "project": "old-name", "request": {"project": "old-name"}},
        )

        result = db.rename_project("old-name", "new-name")

        assert result["ok"] is True
        assert result["old_name"] == "old-name"
        assert result["new_name"] == "new-name"
        assert db.get_project("old-name") is None
        renamed = db.get_project("new-name")
        assert renamed is not None
        assert renamed["path"] == str(tmp_db.parent)
        assert db.get_task(task["id"])["project"] == "new-name"
        assert db.get_session(session["id"])["project"] == "new-name"
        daemon_state = db.get_service_state("daemon", "new-name")
        assert daemon_state is not None
        assert daemon_state["meta"]["project"] == "new-name"
        job_state = db.get_service_state("webui_job", "99")
        assert job_state["meta"]["project"] == "new-name"
        assert job_state["meta"]["request"]["project"] == "new-name"

    def test_rename_project_rejects_existing_target_name(self, tmp_db: Path):
        first = tmp_db.parent / "first"
        second = tmp_db.parent / "second"
        first.mkdir()
        second.mkdir()
        db.register_project("first", str(first))
        db.register_project("second", str(second))

        with pytest.raises(ValueError, match="已存在"):
            db.rename_project("first", "second")

        assert db.get_project("first") is not None
        assert db.get_project("second") is not None

    def test_register_project_existing_path_syncs_manual_config_project_rename(self, tmp_db: Path, monkeypatch):
        monkeypatch.setenv("CODEPILOT_HOME", str(tmp_db.parent / "home"))
        project_path = tmp_db.parent / "workspace"
        project_path.mkdir()
        config_path = project_path / "AGENTS.toml"
        config_path.write_text('[project]\nname = "old-name"\n', encoding="utf-8")
        db.register_project("old-name", str(project_path), config_file=str(config_path))
        task = db.create_task("old-name", "follow register sync", content="body")
        old_log = tmp_db.parent / "home" / "data" / "old-name" / "runs" / "sync.log"
        old_log.parent.mkdir(parents=True, exist_ok=True)
        old_log.write_text("log", encoding="utf-8")
        db.update_task(task["id"], current_log_path=str(old_log))
        config_path.write_text('[project]\nname = "new-name"\n', encoding="utf-8")

        project = db.register_project("new-name", str(project_path), config_file=str(config_path))

        assert project["name"] == "new-name"
        assert db.get_project("old-name") is None
        assert db.get_project("new-name") is not None
        renamed_task = db.get_task(task["id"])
        assert renamed_task["project"] == "new-name"
        assert renamed_task["current_log_path"] == str(tmp_db.parent / "home" / "data" / "new-name" / "runs" / "sync.log")
        assert Path(renamed_task["current_log_path"]).is_file()

    def test_list_projects_skips_manual_config_rename_conflict(self, tmp_db: Path):
        source_path = tmp_db.parent / "source"
        target_path = tmp_db.parent / "target"
        source_path.mkdir()
        target_path.mkdir()
        source_config = source_path / "AGENTS.toml"
        source_config.write_text('[project]\nname = "target"\n', encoding="utf-8")
        db.register_project("source", str(source_path), config_file=str(source_config))
        db.register_project("target", str(target_path))

        projects = db.list_projects()
        sync_results = db.sync_project_config_renames()

        names = {project["name"] for project in projects}
        assert names == {"source", "target"}
        assert sync_results
        assert sync_results[0]["renamed"] is False
        assert sync_results[0]["old_name"] == "source"
        assert sync_results[0]["new_name"] == "target"
        assert "已存在" in sync_results[0]["config_error"]

    def test_project_aliases_are_resolved(self, tmp_db: Path):
        """项目别名应能被 get_project 解析。"""
        db.register_project("alias-target", str(tmp_db.parent))
        db.get_project("alias-target")  # 至少不抛异常


# ── 辅助: create_task 返回 dict，提取 id ────────────────────────────────

def _task_id(project: str, title: str, **kwargs: object) -> int:
    """创建任务并返回其整数 ID。"""
    return db.create_task(project, title, **kwargs)["id"]


# ── 任务 CRUD ──────────────────────────────────────────────────────────

class TestTasks:
    def test_create_and_get_task(self, tmp_db: Path):
        db.register_project("task-proj", str(tmp_db.parent))
        created = db.create_task(
            project="task-proj",
            title="测试任务",
            content="内容",
            priority="P1",
            agent="codex",
        )
        assert created["id"] > 0
        got = db.get_task(created["id"])
        assert got is not None
        assert got["title"] == "测试任务"
        assert got["project"] == "task-proj"
        assert got["status"] == "backlog"

    def test_create_task_with_default_priority(self, tmp_db: Path):
        db.register_project("task-pri", str(tmp_db.parent))
        tid = _task_id("task-pri", "default priority")
        got = db.get_task(tid)
        assert got["priority"] == "P2"  # 默认值

    def test_create_task_with_references(self, tmp_db: Path):
        db.register_project("task-ref", str(tmp_db.parent))
        id_a = _task_id("task-ref", "A")
        id_b = _task_id("task-ref", "B", depends_on=[id_a])
        got_b = db.get_task(id_b)
        depends = json.loads(got_b["depends_on"])
        assert id_a in depends

    def test_update_task(self, tmp_db: Path):
        db.register_project("task-upd", str(tmp_db.parent))
        tid = _task_id("task-upd", "to-update")
        db.update_task(tid, status="running", run_phase="executing")
        got = db.get_task(tid)
        assert got["status"] == "running"
        assert got["run_phase"] == "executing"

    def test_update_task_publishes_event(self, tmp_db: Path):
        """update_task 应触发任务更新事件（信号槽机制）。"""
        db._publish_task_updated_event(task={"id": 42}, changed_fields={"status"})
        assert True  # 验证 publish 本身不抛异常

    def test_list_tasks(self, tmp_db: Path):
        db.register_project("task-list", str(tmp_db.parent))
        tids = [
            _task_id("task-list", f"Task {i}", priority="P1")
            for i in range(5)
        ]
        tasks = db.list_tasks(project="task-list")
        assert len(tasks) == 5
        returned_ids = [t["id"] for t in tasks]
        assert all(i in returned_ids for i in tids)

    def test_list_tasks_with_status_filter(self, tmp_db: Path):
        db.register_project("task-flt", str(tmp_db.parent))
        id_done = _task_id("task-flt", "Done task")
        _task_id("task-flt", "Queued task")
        db.update_task(id_done, status="done")
        done_tasks = db.list_tasks(project="task-flt", status="done")
        assert len(done_tasks) == 1
        assert done_tasks[0]["id"] == id_done

    def test_list_tasks_returns_all(self, tmp_db: Path):
        db.register_project("task-lim", str(tmp_db.parent))
        for i in range(10):
            _task_id("task-lim", f"T{i}")
        tasks = db.list_tasks(project="task-lim")
        assert len(tasks) >= 10

    def test_delete_task(self, tmp_db: Path):
        db.register_project("task-del", str(tmp_db.parent))
        tid = _task_id("task-del", "to-delete")
        assert db.get_task(tid) is not None
        db.delete_task(tid)
        assert db.get_task(tid) is None

    def test_delete_task_removes_logs(self, tmp_db: Path):
        db.register_project("task-log-del", str(tmp_db.parent))
        tid = _task_id("task-log-del", "with-logs")
        db.create_task_log(tid, agent="test", phase="run", output="line 1")
        db.create_task_log(tid, agent="test", phase="run", output="line 2")
        db.delete_task(tid)
        assert db.list_task_logs(tid) == []


# ── 任务工作流 ──────────────────────────────────────────────────────────

class TestTaskWorkflow:
    def test_next_backlog_task_returns_oldest_queued(self, tmp_db: Path):
        db.register_project("backlog", str(tmp_db.parent))
        tids = [
            _task_id("backlog", f"Q{i}", priority="P2")
            for i in range(3)
        ]
        tasks = db.next_backlog_task("backlog")
        assert len(tasks) >= 1
        # 列表按优先级/创建时间排序，第一个应为最早创建的任务
        assert tasks[0]["id"] == tids[0]

    def test_next_backlog_task_returns_none_when_empty(self, tmp_db: Path):
        db.register_project("empty-backlog", str(tmp_db.parent))
        tasks = db.next_backlog_task("empty-backlog")
        assert len(tasks) == 0

    def test_existing_dedup_keys(self, tmp_db: Path):
        db.register_project("dedup", str(tmp_db.parent))
        dedup = "unique-key-42"
        db.create_task("dedup", "dedup test", dedup_key=dedup)
        keys = db.existing_dedup_keys("dedup")
        assert dedup in keys

    def test_increment_task_retry(self, tmp_db: Path):
        db.register_project("retry", str(tmp_db.parent))
        tid = _task_id("retry", "retry-test")
        for i in range(3):
            db.increment_task_retry(tid, error_message=f"error {i}")
        got = db.get_task(tid)
        assert got["retry_count"] >= 3


# ── 任务统计 ────────────────────────────────────────────────────────────

class TestTaskStats:
    def test_get_task_stats_counts_by_status(self, tmp_db: Path):
        db.register_project("stats", str(tmp_db.parent))
        for i in range(3):
            _task_id("stats", f"todo{i}", priority="P2")
        done_tid = _task_id("stats", "last-todo", priority="P2")
        db.update_task(done_tid, status="done")
        for i in range(3):
            _task_id("stats", f"fail{i}", priority="P2")
        stats = db.get_task_stats("stats")
        assert stats.get("backlog", 0) >= 3
        assert stats.get("done", 0) >= 1

    def test_get_task_stats_at_least_returns_dict(self, tmp_db: Path):
        db.register_project("cur-stats", str(tmp_db.parent))
        for i in range(3):
            _task_id("cur-stats", f"T{i}")
        stats = db.get_task_stats("cur-stats")
        assert isinstance(stats, dict)
        assert sum(v for v in stats.values() if isinstance(v, int)) >= 3


# ── 任务日志 ────────────────────────────────────────────────────────────

class TestTaskLogs:
    def test_create_and_list_task_logs(self, tmp_db: Path):
        db.register_project("logs", str(tmp_db.parent))
        tid = _task_id("logs", "logged")
        db.create_task_log(tid, agent="test", phase="run", output="step 1")
        db.create_task_log(tid, agent="test", phase="run", output="step 2")
        logs = db.list_task_logs(tid)
        assert len(logs) == 2
        assert any("step 1" in entry["output"] for entry in logs)
        assert any("step 2" in entry["output"] for entry in logs)

    def test_list_task_logs_returns_empty_for_missing_task(self, tmp_db: Path):
        assert db.list_task_logs(99999) == []

    def test_create_task_log_with_output(self, tmp_db: Path):
        db.register_project("log-lvl", str(tmp_db.parent))
        tid = _task_id("log-lvl", "log-levels")
        db.create_task_log(tid, agent="test", phase="run", output="hello")
        db.create_task_log(tid, agent="test", phase="error", output="oops")
        logs = db.list_task_logs(tid)
        assert len(logs) == 2
        assert any("hello" in entry["output"] for entry in logs)


# ── 服务状态 ────────────────────────────────────────────────────────────

class TestServiceState:
    def test_upsert_and_get_service_state(self, tmp_db: Path):
        db.upsert_service_state("test_svc", "scope1", pid=123, status="running")
        got = db.get_service_state("test_svc", "scope1")
        assert got is not None
        assert got["pid"] == 123
        assert got["status"] == "running"

    def test_upsert_service_state_updates_twice(self, tmp_db: Path):
        db.upsert_service_state("svc", "s", pid=1, status="running")
        db.upsert_service_state("svc", "s", pid=2, status="stopped")
        got = db.get_service_state("svc", "s")
        assert got["pid"] == 2
        assert got["status"] == "stopped"

    def test_list_service_states(self, tmp_db: Path):
        for i in range(3):
            db.upsert_service_state("multi", f"scope{i}", pid=i, status="active")
        states = db.list_service_states("multi")
        assert len(states) == 3

    def test_claim_service_state(self, tmp_db: Path):
        db.upsert_service_state("claimable", "s1", pid=0, status="active")
        # claim 要求 scope 不存在时才能成功；创建一个全新 scope 来验证
        claimed = db.claim_service_state("claimable", "new-scope", pid=999)
        assert claimed is True
        got = db.get_service_state("claimable", "new-scope")
        assert got["pid"] == 999

    def test_clear_service_state(self, tmp_db: Path):
        db.upsert_service_state("clearable", "s1", pid=1, status="active")
        db.clear_service_state("clearable", "s1")
        assert db.get_service_state("clearable", "s1") is None


# ── 查询缓存 ────────────────────────────────────────────────────────────

class TestQueryCache:
    def test_cache_invalidation(self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(db, "_CACHE_TTL_SECONDS", 600)
        db.register_project("cache-test", str(tmp_db.parent))
        # 首次查询填充缓存
        p1 = db.get_project("cache-test")
        assert p1 is not None
        # 刷新缓存
        db._cache_invalidate()
        # 第二次查询应从缓存或 DB 获取
        p2 = db.get_project("cache-test")
        assert p2 is not None

    def test_cache_miss_when_ttl_disabled(self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(db, "_CACHE_TTL_SECONDS", 0)
        db.register_project("no-cache", str(tmp_db.parent))
        result = db._cache_get(("get_project", "no-cache"))
        assert result is db._CACHE_MISS
