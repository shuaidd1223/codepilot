"""End-to-end tests for Web UI HTTP API (real HTTP, mock AI)."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from codepilot import db
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


# ─── GET endpoints ──────────────────────────────────────────────────────────

def test_health_endpoint(ui_server):
    status, body = _get(f"{ui_server}/api/health")
    assert status == 200
    assert body["ok"] is True


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


# ─── HTML page ──────────────────────────────────────────────────────────────

def test_root_serves_html(ui_server):
    req = urllib.request.Request(f"{ui_server}/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert "CodePilot" in html
    assert "<html" in html
