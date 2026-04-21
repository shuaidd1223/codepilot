"""Tests for the webhook HTTP service."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from click.testing import CliRunner

from codepilot import db
from codepilot.commands import webhook as webhook_cmd
from codepilot.webhook import start_webhook_server


def _start_server(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    db.init_db()

    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))

    server = start_webhook_server(host="127.0.0.1", port=0)
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", server


def _get(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post(url: str, body: object) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_webhook_health_endpoint(tmp_path, monkeypatch):
    base_url, server = _start_server(tmp_path, monkeypatch)
    try:
        status, body = _get(f"{base_url}/health")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert body == {"ok": True, "status": "ok", "service": "webhook"}


def test_webhook_tasks_endpoint_creates_backlog_task(tmp_path, monkeypatch):
    base_url, server = _start_server(tmp_path, monkeypatch)
    try:
        status, body = _post(
            f"{base_url}/tasks",
            {
                "project": "demo",
                "title": "来自 webhook 的任务",
                "content": "外部系统投递的任务内容",
                "priority": "P1",
                "agent": "codex",
                "max_retries": 1,
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert status == 201
    assert body["ok"] is True
    task = db.get_task(body["task"]["id"])
    assert task is not None
    assert task["project"] == "demo"
    assert task["title"] == "来自 webhook 的任务"
    assert task["content"] == "外部系统投递的任务内容"
    assert task["priority"] == "P1"
    assert task["source"] == "webhook"
    assert task["status"] == "backlog"


def test_webhook_tasks_endpoint_rejects_bad_payload(tmp_path, monkeypatch):
    base_url, server = _start_server(tmp_path, monkeypatch)
    try:
        status, body = _post(f"{base_url}/tasks", {"project": "demo", "title": ""})
    finally:
        server.shutdown()
        server.server_close()

    assert status == 400
    assert "title" in body["error"]


def test_webhook_unknown_path_returns_404(tmp_path, monkeypatch):
    base_url, server = _start_server(tmp_path, monkeypatch)
    try:
        status, body = _get(f"{base_url}/missing")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 404
    assert "error" in body


def test_webhook_command_starts_server(monkeypatch):
    class _FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            self.closed = True

    calls = []

    def fake_start(*, host: str, port: int):
        calls.append((host, port))
        return _FakeServer()

    monkeypatch.setattr(webhook_cmd, "start_webhook_server", fake_start)

    result = CliRunner().invoke(webhook_cmd.webhook, ["--host", "0.0.0.0", "--port", "9999"])

    assert result.exit_code == 0
    assert calls == [("0.0.0.0", 9999)]
    assert "CodePilot Webhook Server" in result.output
    assert "Webhook 服务已停止" in result.output
