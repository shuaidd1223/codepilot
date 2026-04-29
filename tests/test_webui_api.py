"""End-to-end tests for Web UI HTTP API (real HTTP, mock AI)."""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request

import pytest

from codepilot.storage import database as db
from codepilot.commands import daemon as daemon_cmd
from codepilot.commands import inspect as inspect_cmd
from codepilot.core import progress_bus
from codepilot.core.web_events import publish_task_state_event
from codepilot.webapp import server as webui_mod
from codepilot.core import runtime as runtime_mod
from codepilot.webapp.payloads import daemon_health_payload
from codepilot.webapp.server import start_ui_server


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


def _valid_task_content(title: str) -> str:
    return f"""# {title}

## Task Goal

让 Web UI 批量导入的任务正文完整落库。

## In Scope

- 补齐批量导入任务正文

## Out of Scope

- 不修改执行器

## Forbidden (Hard Boundary)

- 不改数据库结构

## Files In Scope

- `codepilot/webui.py`

## Planning Evidence

来自 Web UI 批量导入场景。

## Acceptance Criteria

- [ ] 批量导入后任务正文完整

## Verification Matrix

| AC | Command | Expected | Evidence |
| :--- | :--- | :--- | :--- |
| AC1 | `codepilot task show <id>` | 可见完整正文 | task detail |

## Reviewer Checkpoints

- 检查模板章节完整
"""


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


def test_internal_event_endpoint_publishes_task_state_event(ui_server):
    progress_bus.clear_subscribers_for_tests()
    events: list[dict] = []
    token = progress_bus.subscribe(lambda event: events.append(event))
    try:
        status, body = _post(
            f"{ui_server}/internal/events",
            {
                "stage": "task-state",
                "type": "started",
                "task_id": 123,
                "level": "info",
                "message": "started",
                "extra": {
                    "project": "demo",
                    "changed_task_ids": [123],
                    "changes": [{"type": "started", "task": {"id": 123, "status": "in_progress"}}],
                },
            },
        )
    finally:
        progress_bus.unsubscribe(token)

    assert status == 200
    assert body["ok"] is True
    assert len(events) == 1
    assert events[0]["stage"] == "task-state"
    assert events[0]["task_id"] == 123
    assert events[0]["extra"]["changed_task_ids"] == [123]


def test_publish_task_state_event_posts_to_registered_webui(ui_server, monkeypatch):
    monkeypatch.setenv("CODEPILOT_ALLOW_TEST_WEB_EVENTS", "1")
    # Use an explicit allow flag because normal pytest runs must not publish
    # task-state events to a developer's real Web UI service.
    progress_bus.clear_subscribers_for_tests()
    task = db.create_task("demo", "cross process task", agent="codex")
    db.update_task(task["id"], status="in_progress", run_phase="builder")
    events: list[dict] = []
    token = progress_bus.subscribe(lambda event: events.append(event))
    try:
        delivered = publish_task_state_event(
            project="demo",
            task=task,
            event="started",
            phase="builder",
            status="in_progress",
            message="任务进入执行队列",
        )
    finally:
        progress_bus.unsubscribe(token)

    assert delivered is True
    assert len(events) == 1
    event = events[0]
    assert event["stage"] == "task-state"
    assert event["task_id"] == task["id"]
    assert event["extra"]["project"] == "demo"
    assert event["extra"]["changed_task_ids"] == [task["id"]]
    assert event["extra"]["changes"][0]["task"]["status"] == "in_progress"
    assert event["extra"]["changes"][0]["task"]["run_phase"] == "builder"


def test_publish_task_state_event_is_suppressed_under_pytest_by_default(ui_server, monkeypatch):
    monkeypatch.delenv("CODEPILOT_ALLOW_TEST_WEB_EVENTS", raising=False)
    progress_bus.clear_subscribers_for_tests()
    task = db.create_task("demo", "suppressed cross process task", agent="codex")
    events: list[dict] = []
    token = progress_bus.subscribe(lambda event: events.append(event))
    try:
        delivered = publish_task_state_event(
            project="demo",
            task=task,
            event="started",
            phase="builder",
            status="in_progress",
            message="测试通知不应进入真实 Web UI",
        )
    finally:
        progress_bus.unsubscribe(token)

    assert delivered is False
    assert events == []


def test_ui_state_mutations_emit_refresh_events(ui_server):
    progress_bus.clear_subscribers_for_tests()
    events: list[dict] = []
    token = progress_bus.subscribe(lambda event: events.append(event))
    try:
        webui_mod._append_event("ui event refresh", project="demo")
        webui_mod._update_job(77, project="demo", status="running", phase="planning")
    finally:
        progress_bus.unsubscribe(token)

    assert [event["stage"] for event in events] == ["ui-state", "ui-state"]
    assert [event["type"] for event in events] == ["event", "job"]
    assert events[0]["extra"]["project"] == "demo"
    assert events[1]["extra"]["payload"]["status"] == "running"


def test_projects_endpoint_lists_registered_project(ui_server):
    status, body = _get(f"{ui_server}/api/projects")
    assert status == 200
    assert "projects" in body
    names = [p["name"] for p in body["projects"]]
    assert "demo" in names


def test_task_template_endpoint_exposes_validation_schema(ui_server):
    status, body = _get(f"{ui_server}/api/task-template")

    assert status == 200
    assert body["ok"] is True
    schema = body["schema"]
    assert schema["validation"]["required_headings"]
    assert "{goal}" in schema["validation"]["placeholder_tokens"]
    assert "content" in schema["validation"]["batch_required_fields"]


def test_project_detail_endpoint(ui_server):
    status, body = _get(f"{ui_server}/api/projects/demo")
    assert status == 200
    assert body.get("selected_project") == "demo"


def test_sessions_endpoint_searches_message_history(ui_server):
    session = db.create_session("demo", title="历史会话")
    db.create_session_message(session["id"], "user", "继续优化任务列表筛选")

    query = urllib.parse.quote("任务列表")
    status, body = _get(f"{ui_server}/api/sessions?project=demo&q={query}")

    assert status == 200
    assert body["searched"] is True
    assert body["matched_sessions"] == 1
    assert body["sessions"][0]["id"] == session["id"]
    assert "任务列表" in body["sessions"][0]["snippet"]


def test_task_detail_404_for_nonexistent(ui_server):
    status, body = _get(f"{ui_server}/api/tasks/99999")
    assert status == 404
    assert "error" in body


def test_unknown_path_returns_404(ui_server):
    status, body = _get(f"{ui_server}/api/nonexistent")
    assert status == 404


# ─── POST /api/tasks ────────────────────────────────────────────────────────

_COMPLIANT_TASK_CONTENT = (
    "# API 创建的测试任务\n\n"
    "## Task Goal\n通过 Web API 创建一条带模板合规 content 的任务。\n\n"
    "## In Scope\n- 写入 backlog\n\n"
    "## Out of Scope\n- 不执行任务\n\n"
    "## Forbidden (Hard Boundary)\n- 不要污染其他项目\n\n"
    "## Files In Scope\n- N/A（仅写库）\n\n"
    "## Planning Evidence\n- 来自 e2e API 测试\n\n"
    "## Acceptance Criteria\n- [ ] 任务出现在 backlog\n\n"
    "## Verification Matrix\n| AC | 命令 | 期望 | 证据 |\n| --- | --- | --- | --- |\n\n"
    "## Reviewer Checkpoints\n- 检查任务模板章节齐全\n"
)


def test_create_task_full_mode_with_compliant_content(ui_server):
    """mode=full：人工自己写完整 task-template content。"""
    status, body = _post(f"{ui_server}/api/tasks", {
        "project": "demo",
        "title": "API 创建的测试任务",
        "priority": "P1",
        "content": _COMPLIANT_TASK_CONTENT,
        "mode": "full",
    })
    assert status == 200
    assert body.get("ok") is True
    assert body.get("task", {}).get("id")
    assert body.get("mode") == "full"

    tasks = db.list_tasks(project="demo")
    assert any("API 创建" in (t.get("title") or "") for t in tasks)


def test_create_task_full_mode_rejects_missing_template_sections(ui_server):
    """mode=full + 缺章节 → 拒绝，不写 backlog。"""
    status, body = _post(f"{ui_server}/api/tasks", {
        "project": "demo",
        "title": "缺章节任务",
        "content": "# 缺章节任务\n\n只有标题和散文，没有 Task Goal 等章节。",
        "mode": "full",
    })
    assert status in (400, 500) or (status == 200 and not body.get("ok", True))
    err = body.get("error") or body
    text = str(err)
    assert "缺少模板必需章节" in text


def test_create_task_ai_complete_mode_invokes_generation(ui_server, monkeypatch):
    """mode=ai_complete：只传 title，由后端调 AI 生成 content。"""
    from codepilot.ai_support import service as ai_mod
    monkeypatch.setattr(ai_mod, "generate_task_content", lambda *a, **kw: _COMPLIANT_TASK_CONTENT)

    status, body = _post(f"{ui_server}/api/tasks", {
        "project": "demo",
        "title": "AI 补全测试任务",
        "mode": "ai_complete",
    })
    assert status == 200
    assert body.get("ok") is True
    assert body.get("mode") == "ai_complete"
    task_id = body.get("task", {}).get("id")
    assert task_id
    saved = db.get_task(task_id)
    assert "Task Goal" in (saved.get("content") or "")


def test_create_task_ai_complete_mode_rejects_when_generation_returns_incomplete(ui_server, monkeypatch):
    """mode=ai_complete + AI 给出缺章节内容 → 必须拒绝，不写 backlog。"""
    from codepilot.ai_support import service as ai_mod
    monkeypatch.setattr(
        ai_mod,
        "generate_task_content",
        lambda *a, **kw: "# 标题\n\n只有几行散文，没有 task-template 9 章节。",
    )

    before = len(db.list_tasks(project="demo"))
    status, body = _post(f"{ui_server}/api/tasks", {
        "project": "demo",
        "title": "AI 输出残缺",
        "mode": "ai_complete",
    })
    assert status in (400, 500) or (status == 200 and not body.get("ok", True))
    text = str(body.get("error") or body)
    assert "缺少模板必需章节" in text
    after = len(db.list_tasks(project="demo"))
    assert before == after, "AI 输出残缺时不能写入 backlog"


def test_create_task_ai_complete_mode_rejects_when_generation_raises(ui_server, monkeypatch):
    """mode=ai_complete + AI 抛 RuntimeError → 必须拒绝并保留可读错误。"""
    from codepilot.ai_support import service as ai_mod

    def _raise(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(ai_mod, "generate_task_content", _raise)

    before = len(db.list_tasks(project="demo"))
    status, body = _post(f"{ui_server}/api/tasks", {
        "project": "demo",
        "title": "provider down",
        "mode": "ai_complete",
    })
    assert status in (400, 500) or (status == 200 and not body.get("ok", True))
    text = str(body.get("error") or body)
    assert "AI 生成任务内容失败" in text
    after = len(db.list_tasks(project="demo"))
    assert before == after


def test_create_task_requirement_mode_redirects_with_error(ui_server):
    """mode=requirement 不应走 /api/tasks，应转 /api/requirements。守卫报错引导。"""
    status, body = _post(f"{ui_server}/api/tasks", {
        "project": "demo",
        "title": "应该走需求规划",
        "mode": "requirement",
    })
    assert status in (400, 500) or (status == 200 and not body.get("ok", True))
    text = str(body.get("error") or body)
    assert "/api/requirements" in text


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

    def fake_submit_goal(project, text, *, category="auto", qa_history=None, original_title="", clarify_answers=None, clarify_questions=None):
        captured.update({
            "project": project,
            "text": text,
            "category": category,
            "qa_history": qa_history,
            "original_title": original_title,
            "clarify_answers": clarify_answers,
            "clarify_questions": clarify_questions,
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
    assert captured["clarify_answers"] == []
    assert captured["clarify_questions"] == []


def test_session_message_endpoint_routes_to_action(ui_server, monkeypatch):
    session = db.create_session("demo", title="chat")
    captured = {}

    def fake_send_session_message(session_id, text, *, category="auto", clarify_answers=None):
        captured.update({
            "session_id": session_id,
            "text": text,
            "category": category,
            "clarify_answers": clarify_answers,
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
        "clarify_answers": [],
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


def test_project_task_service_start_requests_external_daemon_launcher(ui_server, monkeypatch):
    calls = []

    def fake_request_start(project: str):
        calls.append(project)
        return {"running": True, "started": True, "pid": 8765, "project": project}

    monkeypatch.setattr(daemon_cmd, "request_daemon_service_start", fake_request_start)

    status, body = _post(f"{ui_server}/api/projects/demo/tasks/start", {})

    assert status == 200
    assert calls == ["demo"]
    assert body["ok"] is True
    assert body["status"]["pid"] == 8765
    assert "任务执行服务已启动" in body["message"]


def test_project_inspect_service_start_requests_external_launcher(ui_server, monkeypatch):
    calls = []

    def fake_request_start(project: str):
        calls.append(project)
        return {"running": True, "started": True, "pid": 8766, "project": project}

    monkeypatch.setattr(inspect_cmd, "request_inspect_service_start", fake_request_start)

    status, body = _post(f"{ui_server}/api/projects/demo/inspect/start", {})

    assert status == 200
    assert calls == ["demo"]
    assert body["ok"] is True
    assert body["status"]["pid"] == 8766
    assert "巡检服务已启动" in body["message"]


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


def test_import_tasks_via_api_with_valid_template_content(ui_server):
    status, body = _post(
        f"{ui_server}/api/tasks/import",
        {
            "project": "demo",
            "items": [
                {"title": "批量导入任务 1", "content": _valid_task_content("批量导入任务 1")},
                {"title": "批量导入任务 2", "content": _valid_task_content("批量导入任务 2"), "priority": "P1"},
            ],
        },
    )

    assert status == 200
    assert body["ok"] is True
    assert body["count"] == 2
    tasks = db.list_tasks(project="demo")
    assert any(task["title"] == "批量导入任务 1" for task in tasks)
    assert any(task["title"] == "批量导入任务 2" and task["priority"] == "P1" for task in tasks)


def test_import_tasks_via_api_rejects_invalid_template_content(ui_server):
    status, body = _post(
        f"{ui_server}/api/tasks/import",
        {
            "project": "demo",
            "items": [
                {"title": "缺章节任务", "content": "# 缺章节任务\n\n## Task Goal\n\n只有目标，没有剩余章节。"},
            ],
        },
    )

    assert status == 400
    assert "缺少关键章节" in body["error"]


def test_import_tasks_via_api_rejects_missing_content_field(ui_server):
    """批量导入必须每条都有非空 content；占位通道已废除。"""
    status, body = _post(
        f"{ui_server}/api/tasks/import",
        {
            "project": "demo",
            "items": [
                {"title": "只有标题"},
                {"title": "也只有标题", "content": ""},
            ],
        },
    )

    assert status == 400
    err = str(body.get("error") or "")
    assert "缺少 content" in err


# ─── HTML page ──────────────────────────────────────────────────────────────

def test_root_serves_html(ui_server):
    req = urllib.request.Request(f"{ui_server}/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert "CodePilot" in html
    assert "<html" in html

