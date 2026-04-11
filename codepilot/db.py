"""SQLite 数据库层：连接管理、建表、CRUD 操作."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# 数据库路径：~/.codepilot/tasks.db
DB_PATH = Path.home() / ".codepilot" / "tasks.db"


def _get_db_path() -> Path:
    """获取数据库路径（项目根目录的 tasks.db）."""
    db_dir = Path(__file__).parent.parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "tasks.db"


@contextmanager
def get_conn():
    """获取数据库连接的上下文管理器."""
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """初始化数据库表结构."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                name            TEXT PRIMARY KEY,
                path            TEXT NOT NULL UNIQUE,
                base_branch     TEXT NOT NULL DEFAULT 'dev',
                default_mode    TEXT NOT NULL DEFAULT 'dual',
                worktree_base   TEXT,
                config_file     TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                project         TEXT NOT NULL REFERENCES projects(name),
                title           TEXT NOT NULL,
                content         TEXT NOT NULL DEFAULT '',
                agent           TEXT NOT NULL DEFAULT 'dual',
                builder         TEXT,
                reviewer        TEXT,
                priority        TEXT NOT NULL DEFAULT 'P2',
                depends_on      TEXT,
                status          TEXT NOT NULL DEFAULT 'backlog',
                project_path    TEXT NOT NULL,
                branch_name     TEXT,
                worktree_path   TEXT,
                error_message   TEXT,
                delivery_record TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                started_at      TEXT,
                completed_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS task_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     INTEGER NOT NULL REFERENCES tasks(id),
                agent       TEXT,
                phase       TEXT NOT NULL,
                output      TEXT,
                exit_code   INTEGER,
                started_at  TEXT,
                finished_at TEXT,
                duration    INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
        """)


# ─────────────────────────────────────────────────────────
# Projects CRUD
# ─────────────────────────────────────────────────────────

def register_project(
    name: str,
    path: str,
    base_branch: str = "dev",
    default_mode: str = "dual",
    worktree_base: Optional[str] = None,
    config_file: Optional[str] = None,
) -> dict:
    """注册一个新项目到数据库."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR REPLACE INTO projects
                (name, path, base_branch, default_mode, worktree_base, config_file)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, path, base_branch, default_mode, worktree_base, config_file),
        )
        conn.commit()
    return get_project(name)


def get_project(name: str) -> Optional[dict]:
    """根据名称获取项目."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None


def list_projects() -> list[dict]:
    """列出所有已注册项目."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def delete_project(name: str) -> bool:
    """删除项目（同时删除关联任务和日志）."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        conn.execute("DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE project = ?)", (name,))
        conn.execute("DELETE FROM tasks WHERE project = ?", (name,))
        conn.commit()
        return cur.rowcount > 0


# ─────────────────────────────────────────────────────────
# Tasks CRUD
# ─────────────────────────────────────────────────────────

def create_task(
    project: str,
    title: str,
    content: str = "",
    agent: str = "dual",
    priority: str = "P2",
    depends_on: Optional[list[int]] = None,
    project_path: Optional[str] = None,
) -> dict:
    """创建新任务."""
    if not project_path:
        proj = get_project(project)
        project_path = proj["path"] if proj else ""

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks
                (project, title, content, agent, priority, depends_on, project_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project,
                title,
                content,
                agent,
                priority,
                json.dumps(depends_on) if depends_on else None,
                project_path,
            ),
        )
        conn.commit()
        task_id = cur.lastrowid
    return get_task(task_id)


def get_task(task_id: int) -> Optional[dict]:
    """根据 ID 获取任务."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """列出任务，支持按项目和状态过滤."""
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY priority ASC, created_at DESC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def update_task(task_id: int, **fields) -> Optional[dict]:
    """更新任务字段，只更新提供的字段."""
    allowed = {
        "status", "branch_name", "worktree_path", "error_message",
        "delivery_record", "started_at", "completed_at",
        "builder", "reviewer",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_task(task_id)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id]

    with get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        conn.commit()
    return get_task(task_id)


def next_backlog_task(project: str) -> list[dict]:
    """获取下一个待执行任务（按优先级排序，忽略有未完成依赖的任务）."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.* FROM tasks t
            WHERE t.project = ?
              AND t.status = 'backlog'
              AND (
                  t.depends_on IS NULL
                  OR t.depends_on = ''
                  OR NOT EXISTS (
                      SELECT 1 FROM tasks t2, json_each(t.depends_on) j
                      WHERE j.value = CAST(t2.id AS TEXT)
                        AND t2.project = t.project
                        AND t2.status NOT IN ('done', 'failed')
                  )
              )
            ORDER BY
                CASE t.priority
                    WHEN 'P0' THEN 1
                    WHEN 'P1' THEN 2
                    WHEN 'P2' THEN 3
                    WHEN 'P3' THEN 4
                END,
                t.created_at ASC
            LIMIT 1
            """,
            (project,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_task_stats(project: str) -> dict:
    """获取项目的任务统计."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) as count
            FROM tasks
            WHERE project = ?
            GROUP BY status
            """,
            (project,),
        ).fetchall()
        stats = {r["status"]: r["count"] for r in rows}
        return {
            "backlog": stats.get("backlog", 0),
            "in_progress": stats.get("in_progress", 0),
            "done": stats.get("done", 0),
            "failed": stats.get("failed", 0),
            "total": sum(stats.values()),
        }


# ─────────────────────────────────────────────────────────
# Task Logs CRUD
# ─────────────────────────────────────────────────────────

def create_task_log(
    task_id: int,
    agent: str,
    phase: str,
    output: str = "",
    exit_code: Optional[int] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    duration: Optional[int] = None,
) -> dict:
    """创建任务执行日志."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO task_logs
                (task_id, agent, phase, output, exit_code, started_at, finished_at, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, agent, phase, output, exit_code, started_at, finished_at, duration),
        )
        conn.commit()
        log_id = cur.lastrowid
    row = conn.execute("SELECT * FROM task_logs WHERE id = ?", (log_id,)).fetchone()
    return dict(row)


def list_task_logs(task_id: int) -> list[dict]:
    """获取任务的所有执行日志."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY started_at",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
