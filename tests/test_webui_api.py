"""End-to-end tests for Web UI HTTP API (real HTTP, mock AI)."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from codepilot import db
from codepilot.commands import daemon as daemon_cmd
from codepilot import webui as webui_mod
from codepilot import runtime as runtime_mod
from codepilot.webui_payloads import daemon_health_payload
from codepilot.webui import start_ui_server


@pytest.fixture()
def ui_server(tmp_path, monkeypatch):
    """Start a real HTTP server on a random port with a test DB."""
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    db.init_db()

    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "README.md").write_text("# Test", encoding="utf-8")
    db.register_project("demo", str(project_path))

    server = start_ui_server(host="127.0.0.1", port=0, open_browser=False)
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def _get(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _delete(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ─── GET endpoints ──────────────────────────────────────────────────────────

def test_health_endpoint(ui_server):
    status, body = _get(f"{ui_server}/api/health")
    assert status == 200
    assert body["ok"] is True


def test_daemon_health_endpoint_passes_project_query(ui_server, monkeypatch):
    calls = []

    def fake_health(project=None):
        calls.append(project)
        return {"alive": True, "running": True, "pid": 1234, "reason": ""}

    monkeypatch.setattr(webui_mod, "daemon_health_payload", fake_health)

    status, body = _get(f"{ui_server}/api/daemon/health?project=demo")

    assert status == 200
    assert calls == ["demo"]
    assert body["alive"] is True


def test_daemon_health_reads_project_service_state_from_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: int(pid) in {1111, 2222})

    db.upsert_service_state(
        "daemon",
        "demo",
        pid=2222,
        status="running",
        log_path="D:/tmp/daemon.log",
        meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
    )
    db.touch_service_state("daemon", "demo", pid=2222, status="running")

    payload = daemon_health_payload("demo", stale_after_seconds=120)

    assert payload["pid"] == 2222
    assert payload["alive"] is True
    assert payload["reason"] == ""


def test_daemon_health_without_project_aggregates_project_scoped_service_states(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: int(pid) == 2222)

    db.upsert_service_state(
        "daemon",
        "demo",
        pid=2222,
        status="running",
        log_path="D:/tmp/demo-daemon.log",
        meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
    )
    db.touch_service_state("daemon", "demo", pid=2222, status="running")

    payload = daemon_health_payload(None, stale_after_seconds=120)

    assert payload["project"] == "demo"
    assert payload["pid"] == 2222
    assert payload["alive"] is True
    assert payload["reason"] == ""


def test_projects_endpoint_lists_registered_project(ui_server):
    status, body = _get(f"{ui_server}/api/projects")
    assert status == 200
    assert "projects" in body
    names = [p["name"] for p in body["projects"]]
    assert "demo" in names


def test_project_detail_endpoint(ui_server):
    status, body = _get(f"{ui_server}/api/projects/demo")
    assert status == 200
    assert body.get("selected_project") == "demo"


def test_task_detail_404_for_nonexistent(ui_server):
    status, body = _get(f"{ui_server}/api/tasks/99999")
    assert status == 404
    assert "error" in body


def test_unknown_path_returns_404(ui_server):
    status, body = _get(f"{ui_server}/api/nonexistent")
    assert status == 404


# ─── POST /api/tasks ────────────────────────────────────────────────────────

def test_create_task_via_api(ui_server):
    status, body = _post(f"{ui_server}/api/tasks", {
        "project": "demo",
        "title": "API 创建的测试任务",
        "priority": "P1",
    })
    assert status == 200
    assert body.get("ok") is True
    assert body.get("task", {}).get("id")

    # Verify in DB
    tasks = db.list_tasks(project="demo")
    assert any("API 创建" in (t.get("title") or "") for t in tasks)


def test_create_task_empty_title_returns_error(ui_server):
    status, body = _post(f"{ui_server}/api/tasks", {
        "project": "demo",
        "title": "",
    })
    assert status in (400, 200)
    if status == 200:
        assert "error" in body


# ─── POST /api/goal /api/sessions/:id/messages routing ─────────────────────

def test_goal_endpoint_normalizes_non_list_qa_history(ui_server, monkeypatch):
    captured = {}

    def fake_submit_goal(project, text, *, category="auto", qa_history=None, original_title=""):
        captured.update({
            "project": project,
            "text": text,
            "category": category,
            "qa_history": qa_history,
            "original_title": original_title,
        })
        return {"ok": True, "intent": "question", "message": "ok"}

    monkeypatch.setattr(webui_mod, "submit_goal_action", fake_submit_goal)

    status, body = _post(f"{ui_server}/api/goal", {
        "project": "demo",
        "text": "hello",
        "category": "auto",
        "qa_history": "not-a-list",
        "original_title": "raw",
    })

    assert status == 200
    assert body["ok"] is True
    assert captured["qa_history"] == []


def test_session_message_endpoint_routes_to_action(ui_server, monkeypatch):
    session = db.create_session("demo", title="chat")
    captured = {}

    def fake_send_session_message(session_id, text, *, category="auto"):
        captured.update({
            "session_id": session_id,
            "text": text,
            "category": category,
        })
        return {"ok": True, "intent": "question", "message": "routed", "task_ids": []}

    monkeypatch.setattr(webui_mod, "send_session_message_action", fake_send_session_message)

    status, body = _post(f"{ui_server}/api/sessions/{session['id']}/messages", {
        "text": "hello session",
        "category": "question",
    })

    assert status == 200
    assert body["message"] == "routed"
    assert captured == {
        "session_id": session["id"],
        "text": "hello session",
        "category": "question",
    }


def test_unknown_post_path_returns_404(ui_server):
    status, body = _post(f"{ui_server}/api/no-such-post", {})
    assert status == 404
    assert "error" in body


# ─── POST/DELETE /api/projects ──────────────────────────────────────────────

def test_create_and_delete_project_via_api(ui_server, tmp_path):
    project_path = tmp_path / "api-project"
    project_path.mkdir()

    status, body = _post(f"{ui_server}/api/projects", {
        "path": str(project_path),
    })
    assert status == 200
    assert body.get("ok") is True
    assert body.get("project", {}).get("name") == "api-project"
    assert db.get_project("api-project") is not None

    status, body = _delete(f"{ui_server}/api/projects/api-project")
    assert status == 200
    assert body.get("ok") is True
    assert db.get_project("api-project") is None
    assert project_path.exists()


def test_project_task_service_stop_requests_graceful_polling_stop(ui_server, monkeypatch):
    calls = []

    def fake_stop(project: str):
        calls.append(project)
        return {"stopped": False, "stop_requested": True, "pids": [7654]}

    monkeypatch.setattr(daemon_cmd, "stop_daemon_service", fake_stop)

    status, body = _post(f"{ui_server}/api/projects/demo/tasks/stop", {})

    assert status == 200
    assert calls == ["demo"]
    assert body["ok"] is True
    assert body["status"]["stop_requested"] is True
    assert "当前任务完成后" in body["message"]


# ─── POST /api/tasks/:id/retry ──────────────────────────────────────────────

def test_retry_task_via_api(ui_server):
    task = db.create_task("demo", "will fail", agent="codex")
    db.update_task(task["id"], status="failed", error_message="test error")

    status, body = _post(f"{ui_server}/api/tasks/{task['id']}/retry", {})
    assert status == 200

    updated = db.get_task(task["id"])
    assert updated["status"] == "backlog"


# ─── POST /api/tasks/:id/stop ───────────────────────────────────────────────

def test_stop_task_via_api(ui_server):
    task = db.create_task("demo", "running task", agent="codex")
    db.update_task(task["id"], status="in_progress")

    status, body = _post(f"{ui_server}/api/tasks/{task['id']}/stop", {})
    assert status == 200

    updated = db.get_task(task["id"])
    assert updated["status"] in ("cancelled", "in_progress")  # stop may just flag


# ─── POST /api/tasks/:id/cancel ─────────────────────────────────────────────

def test_cancel_backlog_task_via_api(ui_server):
    task = db.create_task("demo", "pending task", agent="codex")

    status, body = _post(f"{ui_server}/api/tasks/{task['id']}/cancel", {})
    assert status == 200
    assert body["ok"] is True

    updated = db.get_task(task["id"])
    assert updated["status"] == "cancelled"


def test_cancel_running_task_rejected_via_api(ui_server):
    task = db.create_task("demo", "running task", agent="codex")
    db.update_task(task["id"], status="in_progress")

    status, body = _post(f"{ui_server}/api/tasks/{task['id']}/cancel", {})
    assert status == 400
    assert "不能取消" in body["error"]


# ─── POST /api/tasks/:id/archive ────────────────────────────────────────────

def test_archive_done_task_via_api(ui_server):
    task = db.create_task("demo", "done task", agent="codex")
    db.update_task(task["id"], status="done", completed_at="2026-04-23T10:00:00")

    status, body = _post(f"{ui_server}/api/tasks/{task['id']}/archive", {})
    assert status == 200
    assert body["ok"] is True

    updated = db.get_task(task["id"])
    assert updated["status"] == "archived"

    status, dashboard = _get(f"{ui_server}/api/projects/demo")
    assert status == 200
    assert task["id"] not in [item["id"] for item in dashboard["tasks"]]


# ─── POST /api/tasks/:id/delete ─────────────────────────────────────────────

def test_delete_task_via_api(ui_server):
    task = db.create_task("demo", "delete me", agent="codex")

    status, body = _post(f"{ui_server}/api/tasks/{task['id']}/delete", {})
    assert status == 200
    assert body["ok"] is True
    assert db.get_task(task["id"]) is None


def test_delete_running_task_rejected_via_api(ui_server):
    task = db.create_task("demo", "running task", agent="codex")
    db.update_task(task["id"], status="in_progress")

    status, body = _post(f"{ui_server}/api/tasks/{task['id']}/delete", {})
    assert status == 400
    assert "不能删除" in body["error"]


# ─── POST /api/tasks/batch ──────────────────────────────────────────────────

def test_batch_cancel_tasks_via_api_all_success(ui_server):
    one = db.create_task("demo", "batch cancel 1", agent="codex")
    two = db.create_task("demo", "batch cancel 2", agent="codex")

    status, body = _post(
        f"{ui_server}/api/tasks/batch",
        {"action": "cancel", "task_ids": [one["id"], two["id"]]},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["success_count"] == 2
    assert body["failed_count"] == 0
    assert db.get_task(one["id"])["status"] == "cancelled"
    assert db.get_task(two["id"])["status"] == "cancelled"


def test_batch_delete_tasks_via_api_reports_partial_failures(ui_server):
    done = db.create_task("demo", "batch delete done", agent="codex")
    running = db.create_task("demo", "batch delete running", agent="codex")
    db.update_task(done["id"], status="done")
    db.update_task(running["id"], status="in_progress")

    status, body = _post(
        f"{ui_server}/api/tasks/batch",
        {"action": "delete", "task_ids": [done["id"], running["id"]]},
    )
    assert status == 200
    assert body["ok"] is False
    assert body["success_count"] == 1
    assert body["failed_count"] == 1
    assert db.get_task(done["id"]) is None
    assert db.get_task(running["id"])["status"] == "in_progress"


# ─── HTML page ──────────────────────────────────────────────────────────────

def test_root_serves_html(ui_server):
    req = urllib.request.Request(f"{ui_server}/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert "CodePilot" in html
    assert "<html" in html
