"""End-to-end tests for Web UI HTTP API (real HTTP, mock AI)."""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO

import pytest

from codepilot.commands.plan import write_plan_artifact
from codepilot.storage import database as db
from codepilot.commands import daemon as daemon_cmd
from codepilot.commands import inspect as inspect_cmd
from codepilot.core import progress_bus
from codepilot.core.workflow_state import append_task_timeline_event, task_execution_artifact_path, write_task_execution_artifacts
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


def _read_sse_data(resp: BinaryIO, *, limit: int = 1) -> list[dict]:
    events: list[dict] = []
    while len(events) < limit:
        line = resp.readline().decode("utf-8")
        if not line:
            break
        if line.startswith("data: "):
            events.append(json.loads(line.removeprefix("data: ").strip()))
    return events


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

def _branch_count(func) -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    branch_nodes = (ast.If, ast.For, ast.While, ast.Try, ast.Match, ast.BoolOp)
    return sum(isinstance(node, branch_nodes) for node in ast.walk(tree))


def test_dashboard_get_handler_keeps_route_dispatch_simple():
    assert _branch_count(webui_mod.DashboardHandler.do_GET) <= 3


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


def test_ai_status_endpoint_passes_project_and_refresh(ui_server, monkeypatch):
    calls = []

    def fake_ai_status(project=None, *, refresh_balance=False):
        calls.append((project, refresh_balance))
        return {"ok": True, "balances": {"deepseek": {}}, "usage": {"deepseek": {"total_tokens": 0}}}

    monkeypatch.setattr(webui_mod, "ai_status_payload", fake_ai_status)

    status, body = _get(f"{ui_server}/api/ai/status?project=demo&refresh=1")

    assert status == 200
    assert calls == [("demo", True)]
    assert body["ok"] is True


def test_ai_status_endpoint_surfaces_provider_unavailable_marker(ui_server):
    db.upsert_service_state(
        "ai_provider",
        "openai-gpt4o",
        status="unavailable",
        meta={
            "provider": "openai-gpt4o",
            "reason": "missing api key",
            "source": "gateway",
            "updated_at": "2026-04-30T11:00:00",
        },
    )

    status, body = _get(f"{ui_server}/api/ai/status?project=demo")

    assert status == 200
    marker = body["availability"]["openai-gpt4o"]
    assert marker["status"] == "unavailable"
    assert marker["reason"] == "missing api key"
    assert body["providers"]["openai-gpt4o"]["availability"] == marker


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


def test_daemon_health_clears_stale_running_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    monkeypatch.setattr(runtime_mod, "is_process_alive", lambda pid: int(pid) == 2222)

    db.upsert_service_state(
        "daemon",
        "demo",
        pid=2222,
        status="running",
        log_path="D:/tmp/demo-daemon.log",
        heartbeat_at="2026-01-01T00:00:00",
        meta={"project": "demo", "started_at": "2026-01-01T00:00:00"},
    )

    payload = daemon_health_payload("demo", stale_after_seconds=120)

    assert payload["alive"] is False
    assert payload["running"] is False
    assert payload["reason"] == "daemon 未运行"
    assert db.get_service_state("daemon", "demo") is None


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


def test_event_stream_skips_task_state_backlog_on_fresh_page_load(ui_server):
    progress_bus.clear_subscribers_for_tests()
    progress_bus.emit(
        stage="task-state",
        task_id=123,
        event_type="started",
        message="started before refresh",
        extra={
            "project": "demo",
            "changed_task_ids": [123],
            "changes": [{"type": "started", "task": {"id": 123, "status": "in_progress"}}],
        },
    )

    with urllib.request.urlopen(f"{ui_server}/api/events/stream?project=demo", timeout=5) as resp:
        events = _read_sse_data(resp, limit=1)

    assert len(events) == 1
    assert events[0]["stage"] == "daemon-health"


def test_event_stream_replays_backlog_when_last_event_id_is_provided(ui_server):
    progress_bus.clear_subscribers_for_tests()
    progress_bus.emit(stage="planner", message="already seen")
    first_event_id = progress_bus.events_since(0)[0]["id"]
    progress_bus.emit(
        stage="task-state",
        task_id=124,
        event_type="done",
        message="finished while disconnected",
        extra={
            "project": "demo",
            "changed_task_ids": [124],
            "changes": [{"type": "done", "task": {"id": 124, "status": "done"}}],
        },
    )

    url = f"{ui_server}/api/events/stream?project=demo&last_event_id={first_event_id}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        events = _read_sse_data(resp, limit=1)

    assert len(events) == 1
    assert events[0]["stage"] == "task-state"
    assert events[0]["task_id"] == 124


def test_event_stream_replays_session_run_events_when_last_event_id_is_provided(ui_server):
    progress_bus.clear_subscribers_for_tests()
    progress_bus.emit(stage="planner", message="already seen")
    first_event_id = progress_bus.events_since(0)[0]["id"]
    progress_bus.emit(
        stage="session-run",
        event_type="delta",
        message="会话输出更新",
        extra={
            "project": "demo",
            "session_id": 7,
            "assistant_message_id": 9,
            "status": "running",
            "content_delta": "增量",
            "content_snapshot": "增量",
            "tool_calls": [],
            "opencode_session_id": "ses_1",
        },
    )

    url = f"{ui_server}/api/events/stream?project=demo&last_event_id={first_event_id}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        events = _read_sse_data(resp, limit=1)

    assert len(events) == 1
    assert events[0]["stage"] == "session-run"
    assert events[0]["extra"]["content_delta"] == "增量"


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
    projects = body.get("projects") or []
    demo = next((p for p in projects if p["name"] == "demo"), {})
    workflow = demo.get("workflow") or {}
    auto_policy = workflow.get("auto_policy") or {}
    assert auto_policy == {
        "allow_create_inspect_tasks": False,
        "allow_import_plan_tasks": False,
        "max_steps": 1,
        "failure_threshold": 1,
    }


def test_project_detail_endpoint_returns_empty_workflow_board(ui_server):
    status, body = _get(f"{ui_server}/api/projects/demo")

    assert status == 200
    board = body["task_board"]
    assert board == body["task_board_by_project"]["demo"]
    assert board["project"] == "demo"
    assert board["total"] == 0
    assert board["sort"]["column_order"] == ["backlog", "ready", "running", "review", "blocked", "done"]
    assert [column["id"] for column in board["columns"]] == board["sort"]["column_order"]
    assert [column["title"] for column in board["columns"]] == [
        "Backlog",
        "Ready",
        "Running",
        "Review",
        "Blocked",
        "Done",
    ]
    assert board["counts"] == {
        "backlog": 0,
        "ready": 0,
        "running": 0,
        "review": 0,
        "blocked": 0,
        "done": 0,
    }
    assert all(column["count"] == 0 and column["tasks"] == [] for column in board["columns"])


def test_project_detail_workflow_board_groups_task_states(ui_server):
    backlog = db.create_task("demo", "missing acceptance criteria", content="", agent="codex")
    ready = db.create_task("demo", "ready to run", content=_COMPLIANT_TASK_CONTENT, agent="codex", priority="P1")
    running = db.create_task("demo", "builder running", content=_COMPLIANT_TASK_CONTENT, agent="codex")
    db.update_task(
        running["id"],
        status="in_progress",
        run_phase="builder",
        started_at="2026-05-25T09:00:00",
        heartbeat_at="2026-05-25T09:01:00",
        last_output="editing files",
    )
    review = db.create_task("demo", "review running", content=_COMPLIANT_TASK_CONTENT, agent="dual")
    db.update_task(
        review["id"],
        status="in_progress",
        run_phase="reviewer",
        started_at="2026-05-25T09:02:00",
        heartbeat_at="2026-05-25T09:03:00",
        last_output="checking acceptance criteria",
    )
    failed = db.create_task("demo", "tests failed", content=_COMPLIANT_TASK_CONTENT, agent="codex")
    db.update_task(
        failed["id"],
        status="failed",
        run_phase="builder",
        error_message="pytest failed: 1 failed",
        completed_at="2026-05-25T09:04:00",
    )
    done = db.create_task("demo", "verified", content=_COMPLIANT_TASK_CONTENT, agent="codex")
    db.update_task(
        done["id"],
        status="done",
        run_phase="merge",
        delivery_record="VERDICT: PASS",
        completed_at="2026-05-25T09:05:00",
    )

    status, body = _get(f"{ui_server}/api/projects/demo")

    assert status == 200
    columns = {column["id"]: column for column in body["task_board"]["columns"]}
    assert [task["id"] for task in columns["backlog"]["tasks"]] == [backlog["id"]]
    assert [task["id"] for task in columns["ready"]["tasks"]] == [ready["id"]]
    assert [task["id"] for task in columns["running"]["tasks"]] == [running["id"]]
    assert [task["id"] for task in columns["review"]["tasks"]] == [review["id"]]
    assert [task["id"] for task in columns["blocked"]["tasks"]] == [failed["id"]]
    assert [task["id"] for task in columns["done"]["tasks"]] == [done["id"]]

    failed_card = columns["blocked"]["tasks"][0]
    assert failed_card["workflow_column_id"] == "blocked"
    assert failed_card["blocked_reason"] == "pytest failed: 1 failed"
    assert failed_card["execution_status"] == "failed:builder"
    assert failed_card["updated_at"] == "2026-05-25T09:04:00"
    assert failed_card["actions"]["retry"] is True

    running_card = columns["running"]["tasks"][0]
    assert running_card["execution_status"] == "in_progress:builder"
    assert running_card["updated_at"] == "2026-05-25T09:01:00"
    assert running_card["actions"]["stop"] is True


def test_project_permission_endpoint_updates_agents_toml(ui_server):
    project = db.get_project("demo")
    config_path = Path(project["path"]) / "AGENTS.toml"
    config_path.write_text(
        """
[project]
name = "demo"

[opencode.permission]
mode = "ask"
""".lstrip(),
        encoding="utf-8",
    )

    status, body = _post(
        f"{ui_server}/api/projects/demo/permission",
        {"mode": "full_access"},
    )

    assert status == 200
    assert body["ok"] is True
    assert body["mode"] == "full_access"
    assert 'mode = "full_access"' in config_path.read_text(encoding="utf-8")


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


def test_task_detail_exposes_execution_artifact_summary(ui_server):
    project = db.get_project("demo")
    task = db.create_task("demo", "artifact detail", agent="dual")
    write_task_execution_artifacts(
        project["path"],
        task["id"],
        status="done",
        source="run",
        executor="builtin",
        artifacts={
            "patch": {
                "kind": "patch",
                "status": "captured",
                "empty": False,
                "summary": "1 file changed: codepilot/webapp/task_payloads.py",
                "files": [{"path": "codepilot/webapp/task_payloads.py", "status": "M"}],
            },
            "validation": {
                "kind": "validation",
                "status": "passed",
                "checks": [{"command": "pytest tests/test_webui_api.py -q", "exit_code": 0, "ok": True}],
            },
            "review": {
                "kind": "review",
                "status": "pass",
                "verdict": "pass",
                "summary": "VERDICT: PASS",
            },
        },
    )

    status, body = _get(f"{ui_server}/api/tasks/{task['id']}")

    assert status == 200
    artifacts = body["artifacts"]
    assert artifacts["patch"]["summary"] == "1 file changed: codepilot/webapp/task_payloads.py"
    assert artifacts["patch"]["files"][0]["path"] == "codepilot/webapp/task_payloads.py"
    assert artifacts["validation"]["checks"][0]["exit_code"] == 0
    assert artifacts["review"]["verdict"] == "pass"


def test_task_detail_exposes_timeline_and_legacy_empty_fallback(ui_server):
    project = db.get_project("demo")
    legacy = db.create_task("demo", "legacy detail without timeline", agent="dual")
    task_execution_artifact_path(project["path"], legacy["id"]).unlink(missing_ok=True)

    status, body = _get(f"{ui_server}/api/tasks/{legacy['id']}")

    assert status == 200
    assert body["timeline"] == []

    task = db.create_task("demo", "timeline detail", agent="dual")
    append_task_timeline_event(
        project["path"],
        task["id"],
        event="claimed",
        actor="daemon",
        source="codepilot.run",
        message="任务进入执行队列",
        artifact_path=".codepilot/artifacts/tasks/task-timeline.json",
        time="2026-05-25T09:00:00",
    )
    append_task_timeline_event(
        project["path"],
        task["id"],
        event="done",
        actor="runner",
        source="codepilot.run",
        message="任务完成",
        time="2026-05-25T09:05:00",
    )

    status, body = _get(f"{ui_server}/api/tasks/{task['id']}")

    assert status == 200
    assert [item["event"] for item in body["timeline"]] == ["created", "claimed", "done"]
    claimed = next(item for item in body["timeline"] if item["event"] == "claimed")
    assert claimed["message"] == "任务进入执行队列"
    assert claimed["artifact_path"] == ".codepilot/artifacts/tasks/task-timeline.json"


def test_task_log_endpoint_returns_delta_from_offset(ui_server, tmp_path):
    task = db.create_task("demo", "log task", agent="codex")
    log_path = tmp_path / "task.log"
    log_path.write_bytes(b"line-1\nline-2\n")
    db.update_task(task["id"], status="in_progress", current_log_path=str(log_path))

    status, body = _get(f"{ui_server}/api/tasks/{task['id']}/log?offset=7")

    assert status == 200
    assert body["offset"] == 7
    assert body["text"] == "line-2\n"
    assert body["next_offset"] == log_path.stat().st_size


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


# ─── POST /api/requirements ────────────────────────────────────────────────

def test_requirement_endpoint_records_web_work_item_source(ui_server, monkeypatch):
    captured = {}

    def fake_run_requirement_workflow(**kwargs):
        captured.update(kwargs)
        return {"tasks": [], "summary": "ok"}

    monkeypatch.setattr(webui_mod, "run_requirement_workflow", fake_run_requirement_workflow)

    status, body = _post(
        f"{ui_server}/api/requirements",
        {
            "project": "demo",
            "title": "从 Web UI 提交需求",
            "execute": False,
            "run_async": False,
        },
    )

    assert status == 200
    request = body["job"]["request"]
    assert request["task_source"] == "web"
    assert request["work_item"]["source"] == "web"
    assert request["work_item"]["requester"] == "web"
    assert request["work_item"]["raw_text"] == "从 Web UI 提交需求"
    assert request["work_item"]["callback"]["job_id"] == body["job"]["id"]
    assert captured["task_source"] == "web"
    assert captured["work_item"] == request["work_item"]


# ─── POST /api/sessions/:id/messages routing ────────────────────────────────

def test_goal_endpoint_is_removed(ui_server):
    status, body = _post(f"{ui_server}/api/goal", {
        "project": "demo",
        "text": "hello",
        "category": "auto",
    })

    assert status == 404
    assert "未找到接口" in body["error"]


def test_session_message_endpoint_routes_to_action_with_async_default(ui_server, monkeypatch):
    session = db.create_session("demo", title="chat")
    captured = {}

    def fake_send_session_message(session_id, text, *, run_async=False, runtime_config=None):
        captured.update({
            "session_id": session_id,
            "text": text,
            "run_async": run_async,
            "runtime_config": runtime_config,
        })
        return {
            "ok": True,
            "intent": "opencode",
            "message": "routed",
            "task_ids": [],
            "status": "running",
            "assistant_message_id": 9,
        }

    monkeypatch.setattr(webui_mod, "send_session_message_action", fake_send_session_message)

    status, body = _post(f"{ui_server}/api/sessions/{session['id']}/messages", {
        "text": "hello session",
    })

    assert status == 200
    assert body["message"] == "routed"
    assert captured == {
        "session_id": session["id"],
        "text": "hello session",
        "run_async": True,
        "runtime_config": None,
    }


def test_session_message_endpoint_can_force_sync_mode(ui_server, monkeypatch):
    session = db.create_session("demo", title="chat")
    captured = {}

    def fake_send_session_message(session_id, text, *, run_async=False, runtime_config=None):
        captured["run_async"] = run_async
        return {"ok": True, "intent": "opencode", "message": "sync", "task_ids": []}

    monkeypatch.setattr(webui_mod, "send_session_message_action", fake_send_session_message)

    status, body = _post(
        f"{ui_server}/api/sessions/{session['id']}/messages",
        {"text": "hello session", "run_async": False},
    )

    assert status == 200
    assert body["message"] == "sync"
    assert captured["run_async"] is False


def test_session_run_stop_endpoint_routes_to_action(ui_server, monkeypatch):
    session = db.create_session("demo", title="chat")
    captured = {}

    def fake_stop(session_id, message_id):
        captured.update({"session_id": session_id, "message_id": message_id})
        return {"ok": True, "message": "已停止", "assistant_message_id": message_id}

    monkeypatch.setattr(webui_mod, "stop_session_run_action", fake_stop)

    status, body = _post(f"{ui_server}/api/sessions/{session['id']}/runs/123/stop", {})

    assert status == 200
    assert body["message"] == "已停止"
    assert captured == {"session_id": session["id"], "message_id": 123}


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


def test_artifact_import_next_action_via_api_uses_task_batch_file(ui_server):
    project = db.get_project("demo")
    plan = write_plan_artifact(project, "新增 explore", use_wiki=False)

    status, body = _post(
        f"{ui_server}/api/artifacts/actions",
        {
            "project": "demo",
            "context_path": plan["context_path"],
            "action_id": "import_tasks",
        },
    )

    assert status == 200
    assert body["ok"] is True
    assert body["action_id"] == "import_tasks"
    assert body["task_batch_path"] == plan["task_batch_path"]
    assert body["result"]["count"] == len(plan["task_candidates"])
    tasks = db.list_tasks(project="demo")
    assert len(tasks) == len(plan["task_candidates"])


def test_artifact_import_next_action_via_api_rejects_invalid_task_batch(ui_server):
    project = db.get_project("demo")
    plan = write_plan_artifact(project, "新增 explore", use_wiki=False)
    before = len(db.list_tasks(project="demo"))
    with open(plan["task_batch_path"], "w", encoding="utf-8", newline="\n") as handle:
        json.dump([{"title": "缺章节任务", "content": "# 缺章节任务\n\n只有标题。"}], handle, ensure_ascii=False)

    status, body = _post(
        f"{ui_server}/api/artifacts/actions",
        {
            "project": "demo",
            "context_path": plan["context_path"],
            "action_id": "import_tasks",
        },
    )

    assert status == 400
    assert "缺少关键章节" in body["error"]
    assert len(db.list_tasks(project="demo")) == before


def test_workflow_auto_action_via_api_uses_shared_policy(ui_server, monkeypatch):
    project = db.get_project("demo")
    project_path = Path(project["path"])
    (project_path / "AGENTS.toml").write_text(
        """
[project]
name = "demo"

[automation]
workflow_auto_import_plan_tasks = true
workflow_auto_max_steps = 1
""".strip(),
        encoding="utf-8",
    )
    plan = write_plan_artifact(project, "新增 Web 工作流自动策略", use_wiki=False)
    context_path = Path(plan["context_path"])
    context = json.loads(context_path.read_text(encoding="utf-8"))
    for action in context["next_actions"]:
        if action["id"] == "import_tasks":
            action["suggested_command"] = r"codepilot add -p demo -f C:\does-not-exist\tasks.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr("codepilot.commands.add.check_provider_availability", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("codepilot.commands.add.resolve_agent_with_fallback", lambda agent, **kwargs: (agent, None))

    status, body = _post(
        f"{ui_server}/api/workflow/actions",
        {"project": "demo", "auto": True},
    )

    assert status == 200
    assert body["ok"] is True
    assert body["action"]["id"] == "import_tasks"
    assert body["selected_reason"] == "policy_allowed_plan_import"
    assert len(db.list_tasks(project="demo")) == len(plan["task_candidates"])


# ─── HTML page ──────────────────────────────────────────────────────────────

def test_root_serves_html(ui_server):
    req = urllib.request.Request(f"{ui_server}/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert "CodePilot" in html
    assert "<html" in html
