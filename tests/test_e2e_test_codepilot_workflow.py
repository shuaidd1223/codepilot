"""Explicit E2E acceptance checks for the external test-codepilot sandbox.

The sandbox portion is opt-in because it uses the real global CodePilot DB and
the mutable project at ``D:\tmp\test-codepilot``. Run it with:

    python -m pytest tests/test_e2e_test_codepilot_workflow.py -q --run-e2e-sandbox
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import pytest
from click.testing import CliRunner

from codepilot.core import progress_bus
from codepilot.storage import database as db
from codepilot.webapp import action_requirements
from codepilot.webapp import server as webui_mod
from codepilot.webapp.server import start_ui_server


PROJECT_NAME = "test-codepilot"
SANDBOX_PATH = Path(r"D:\tmp\test-codepilot")
E2E_TITLE_PREFIX = "E2E hello acceptance"


@dataclass
class CommandRun:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class E2ERun:
    sandbox: Path
    run_id: str
    run_dir: Path
    snapshot: dict[str, bytes]
    created_task_ids: list[int] = field(default_factory=list)
    created_job_ids: list[int] = field(default_factory=list)

    def write_json(self, name: str, payload: object) -> Path:
        target = self.run_dir / _artifact_name(name, ".json")
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return target

    def write_text(self, name: str, text: str) -> Path:
        target = self.run_dir / _artifact_name(name, ".txt")
        target.write_text(text, encoding="utf-8", errors="replace")
        return target

    def write_command(self, name: str, run: CommandRun) -> Path:
        return self.write_json(
            name,
            {
                "args": run.args,
                "returncode": run.returncode,
                "stdout": run.stdout,
                "stderr": run.stderr,
            },
        )


def _artifact_name(name: str, suffix: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in name)
    return safe if safe.endswith(suffix) else f"{safe}{suffix}"


def _validate_sandbox(path: Path = SANDBOX_PATH, project_name: str = PROJECT_NAME) -> list[str]:
    issues: list[str] = []
    if not path.exists():
        return [f"沙箱路径不存在：{path}"]
    if not path.is_dir():
        issues.append(f"沙箱路径不是目录：{path}")

    config_path = path / "AGENTS.toml"
    if not config_path.is_file():
        issues.append(f"缺少 AGENTS.toml：{config_path}")
    else:
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            issues.append(f"AGENTS.toml 无法解析：{exc}")
        else:
            actual = str((config.get("project") or {}).get("name") or "")
            if actual != project_name:
                issues.append(f"AGENTS.toml project.name 应为 {project_name!r}，实际为 {actual!r}")

    if not (path / ".git").exists():
        issues.append(f"沙箱不是 git 工作区：{path}")
    if not (path / "main.py").is_file():
        issues.append(f"缺少 hello 场景入口：{path / 'main.py'}")
    return issues


def _git_run(root: Path, args: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def snapshot_tracked_files(root: Path) -> dict[str, bytes]:
    result = _git_run(root, ["ls-files", "-z"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git ls-files failed")
    snapshot: dict[str, bytes] = {}
    for rel in result.stdout.split("\0"):
        if not rel:
            continue
        file_path = root / rel
        if file_path.is_file():
            snapshot[rel] = file_path.read_bytes()
    return snapshot


def restore_tracked_files(root: Path, snapshot: dict[str, bytes]) -> None:
    for rel, data in snapshot.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _git_status_short(root: Path) -> str:
    result = _git_run(root, ["status", "--short"])
    return result.stdout if result.returncode == 0 else result.stderr


def _run_codepilot(args: list[str], *, cwd: Path = SANDBOX_PATH, timeout: int = 60) -> CommandRun:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("CODEPILOT_DESKTOP_NOTIFY", "0")
    completed = subprocess.run(
        [sys.executable, "-m", "codepilot", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return CommandRun(
        args=[sys.executable, "-m", "codepilot", *args],
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _parse_first_json_object(text: str) -> dict:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError("输出中没有可解析的 JSON object")


def _assert_successful_json_command(run: CommandRun) -> dict:
    assert run.returncode == 0, run.stderr or run.stdout
    payload = _parse_first_json_object(f"{run.stdout}\n{run.stderr}")
    assert payload.get("ok") is True, payload
    assert isinstance(payload.get("data"), dict), payload
    return payload


def _http_json(url: str, *, method: str = "GET", body: dict | None = None, timeout: int = 10) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _assert_dashboard_payload(payload: dict, *, project: str) -> None:
    assert payload.get("selected_project") == project
    assert isinstance(payload.get("projects"), list)
    assert isinstance(payload.get("tasks"), list)
    assert isinstance(payload.get("jobs"), list)
    assert isinstance(payload.get("tasks_by_project"), dict)
    assert project in payload["tasks_by_project"]


def _assert_task_detail_payload(payload: dict, *, task_id: int) -> None:
    task = payload.get("task") if isinstance(payload.get("task"), dict) else payload
    assert isinstance(task, dict), payload
    assert task.get("id") == task_id
    assert isinstance(payload.get("recovery_hints"), list), payload
    assert "log_text" in payload, payload


def _assert_log_payload(payload: dict, *, require_text: bool = True) -> None:
    assert {"offset", "next_offset", "size", "text", "done", "path"} <= set(payload)
    if require_text:
        assert payload["text"], payload


def _task_action_toast_level(status: int, payload: dict | None = None, *, raised: bool = False) -> str:
    if raised or status >= 400:
        return "error"
    body = payload or {}
    if body.get("ok") is False or body.get("service_error"):
        return "warning"
    return "success"


@contextlib.contextmanager
def _pushd(path: Path):
    old_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old_cwd)


@contextlib.contextmanager
def _ui_server(run: E2ERun, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        action_requirements,
        "_request_task_service_start",
        lambda project: ({"running": True, "started": False, "project": project}, ""),
    )
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOBS.clear()
    server = start_ui_server(host="127.0.0.1", port=0, open_browser=False)
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run.write_json("ui-server", {"url": f"http://127.0.0.1:{port}", "port": port})
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _task_content(title: str) -> str:
    return f"""# {title}

---

## Metadata

| Field | Value |
| :--- | :--- |
| Agent | dual |
| Priority | P2 |
| Depends on | none |
| Risk Level | low |
| Scope Budget | 1 file / no production changes |
| Owner | e2e |

---

## Task Goal

Validate that the test-codepilot hello sandbox can carry a task through CLI and Web UI task operations without invoking an AI executor.

## In Scope

- Use the existing `main.py` hello scenario as task context.
- Exercise task visibility, logs, retry, promote, cancel, resume, archive, and delete paths.

## Out of Scope

- Do not run a real builder or reviewer agent.
- Do not change project source code.

## Forbidden (Hard Boundary)

- Do not commit sandbox changes.
- Do not edit files outside `D:\\tmp\\test-codepilot`.

## Files In Scope

- `main.py`

## Planning Evidence

The sandbox contains `main.py`, AGENTS.toml project name `test-codepilot`, and is registered in the CodePilot task database.

---

## Risk Assessment

- [ ] Touches database schema / migrations
- [ ] Touches authentication / middleware
- [ ] Touches core directory structure
- [ ] Needs Owner confirmation

## Risks & Notes

- This task is created only for E2E acceptance and should be cleaned up after the run.

---

## Acceptance Criteria

- [ ] The task appears in CLI status and Web UI project detail.
- [ ] Task logs are visible through CLI and Web UI APIs.
- [ ] Existing task operations return clear success or warning feedback.

## Verification Matrix

| AC | Command | Expected | Evidence |
| :--- | :--- | :--- | :--- |
| AC1 | `codepilot task show <id>` | Shows the created task | e2e artifacts |
| AC2 | `codepilot task logs <id>` | Shows seeded log lines | e2e artifacts |
| AC3 | Web task action APIs | Return success or warning payloads | e2e artifacts |

## Execution Order

1. Red: the E2E harness asserts the behavior before cleanup.
2. Green: no production implementation is executed by this sandbox task.
3. Refactor: no source refactor is in scope.
4. Verify: collect CLI and Web API artifacts under `.codepilot/e2e-runs`.

---

## Reviewer Checkpoints

- Confirm no source file changed in the sandbox.
- Confirm operation feedback is explicit and copyable where manual daemon startup is required.
- End with `VERDICT: PASS` or `VERDICT: FAIL`.
"""


def _create_task_with_cli(run: E2ERun) -> dict:
    title = f"{E2E_TITLE_PREFIX} {run.run_id}"
    batch_file = run.run_dir / "task-batch.json"
    batch_file.write_text(
        json.dumps(
            [
                {
                    "title": title,
                    "priority": "P2",
                    "agent": "dual",
                    "content": _task_content(title),
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = _run_codepilot(
        ["add", "-p", PROJECT_NAME, "-f", str(batch_file), "--json"],
        cwd=run.sandbox,
        timeout=60,
    )
    run.write_command("cli-add-task", result)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = _parse_first_json_object(f"{result.stdout}\n{result.stderr}")
    imported = payload.get("imported") or (payload.get("data") or {}).get("imported") or []
    assert imported and imported[0].get("id"), payload
    task = imported[0]
    run.created_task_ids.append(int(task["id"]))
    return task


def _seed_task_logs(run: E2ERun, task_id: int) -> Path:
    log_path = run.run_dir / f"task-{task_id}.log"
    log_text = f"e2e log for task {task_id}\nhello from {run.run_id}\n"
    log_path.write_text(log_text, encoding="utf-8")
    db.init_db()
    db.update_task(
        task_id,
        current_log_path=str(log_path),
        last_output=log_text,
    )
    db.create_task_log(task_id, agent="e2e", phase="builder", output=log_text, exit_code=0)
    return log_path


def _run_read_only_status_chain(run: E2ERun, monkeypatch: pytest.MonkeyPatch) -> None:
    doctor = _run_codepilot(["doctor", "-p", PROJECT_NAME, "--services", "--json"], cwd=run.sandbox, timeout=90)
    run.write_command("cli-doctor-services", doctor)
    assert doctor.returncode == 0, doctor.stderr or doctor.stdout
    doctor_payload = _parse_first_json_object(f"{doctor.stdout}\n{doctor.stderr}")
    assert isinstance(doctor_payload.get("data", {}).get("checks"), list), doctor_payload

    status = _run_codepilot(["status", "-p", PROJECT_NAME, "--json"], cwd=run.sandbox)
    run.write_command("cli-status", status)
    status_payload = _assert_successful_json_command(status)
    assert status_payload["data"]["project"] == PROJECT_NAME

    trace = _run_codepilot(["trace", "-p", PROJECT_NAME, "--limit", "20", "--json"], cwd=run.sandbox)
    run.write_command("cli-trace", trace)
    trace_payload = _assert_successful_json_command(trace)
    assert trace_payload["data"]["project"] == PROJECT_NAME
    assert isinstance(trace_payload["data"].get("events"), list)

    from codepilot.commands import inspect as inspect_cmd

    monkeypatch.setattr(inspect_cmd, "_call_llm", lambda *args, **kwargs: {"candidates": []})
    with _pushd(run.sandbox):
        inspect_result = CliRunner().invoke(
            inspect_cmd.inspect,
            ["-p", PROJECT_NAME, "--once", "--dry-run", "--write-workflow", "--json"],
            catch_exceptions=False,
        )
    run.write_json(
        "cli-inspect-dry-run",
        {
            "args": ["codepilot", "inspect", "-p", PROJECT_NAME, "--once", "--dry-run", "--write-workflow", "--json"],
            "returncode": inspect_result.exit_code,
            "stdout": inspect_result.output,
        },
    )
    assert inspect_result.exit_code == 0, inspect_result.output
    inspect_payload = _parse_first_json_object(inspect_result.output)
    assert inspect_payload.get("ok") is True
    assert inspect_payload["data"]["project"] == PROJECT_NAME
    assert inspect_payload["data"].get("workflow_context", {}).get("context_path")


def _assert_cli_task_visibility(run: E2ERun, task_id: int) -> None:
    show = _run_codepilot(["task", "show", str(task_id), "--json"], cwd=run.sandbox)
    run.write_command("cli-task-show", show)
    show_payload = _assert_successful_json_command(show)
    assert show_payload["data"]["task"]["id"] == task_id

    logs = _run_codepilot(["task", "logs", str(task_id), "--full"], cwd=run.sandbox)
    run.write_command("cli-task-logs", logs)
    assert logs.returncode == 0, logs.stderr or logs.stdout
    assert "e2e log for task" in logs.stdout


def _assert_api_visibility(base_url: str, run: E2ERun, task_id: int, log_path: Path) -> None:
    status, dashboard = _http_json(f"{base_url}/api/projects/{urllib.parse.quote(PROJECT_NAME)}")
    run.write_json("api-project-detail", {"status": status, "body": dashboard})
    assert status == 200
    _assert_dashboard_payload(dashboard, project=PROJECT_NAME)
    assert task_id in [int(item["id"]) for item in dashboard["tasks_by_project"][PROJECT_NAME]]

    status, detail = _http_json(f"{base_url}/api/tasks/{task_id}")
    run.write_json("api-task-detail", {"status": status, "body": detail})
    assert status == 200
    _assert_task_detail_payload(detail, task_id=task_id)
    assert "e2e log for task" in detail.get("log_text", "")

    status, log_delta = _http_json(f"{base_url}/api/tasks/{task_id}/log?offset=0")
    run.write_json("api-task-log", {"status": status, "body": log_delta})
    assert status == 200
    _assert_log_payload(log_delta)
    assert log_delta["path"] == str(log_path)


def _assert_event_stream(base_url: str, task_id: int) -> None:
    progress_bus.clear_subscribers_for_tests()
    progress_bus.emit(stage="planner", message="already seen")
    first_event_id = progress_bus.events_since(0)[0]["id"]
    progress_bus.emit(
        stage="task-state",
        task_id=task_id,
        event_type="done",
        message="e2e task event replay",
        extra={
            "project": PROJECT_NAME,
            "changed_task_ids": [task_id],
            "changes": [{"type": "done", "task": {"id": task_id, "status": "done"}}],
        },
    )

    url = f"{base_url}/api/events/stream?project={urllib.parse.quote(PROJECT_NAME)}&last_event_id={first_event_id}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        events = _read_sse_data(resp, limit=1)
    assert events and events[0]["stage"] == "task-state"
    assert events[0]["task_id"] == task_id


def _assert_task_actions(base_url: str, run: E2ERun, task_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    db.update_task(task_id, status="failed", error_message="e2e synthetic failure")
    cli_retry = _run_codepilot(["task", "retry", str(task_id)], cwd=run.sandbox)
    run.write_command("cli-task-retry", cli_retry)
    assert cli_retry.returncode == 0, cli_retry.stderr or cli_retry.stdout
    assert "backlog" in cli_retry.stdout

    db.update_task(task_id, status="failed", error_message="e2e synthetic failure")
    status, retry_body = _http_json(f"{base_url}/api/tasks/{task_id}/retry", method="POST", body={})
    run.write_json("api-task-retry", {"status": status, "body": retry_body})
    assert status == 200
    assert retry_body["ok"] is True
    assert _task_action_toast_level(status, retry_body) == "success"
    assert db.get_task(task_id)["status"] == "backlog"

    status, promote_body = _http_json(f"{base_url}/api/tasks/{task_id}/promote", method="POST", body={})
    run.write_json("api-task-promote", {"status": status, "body": promote_body})
    assert status == 200
    assert promote_body["ok"] is True
    assert db.get_task(task_id)["priority"] == "P0"

    cli_cancel = _run_codepilot(["task", "cancel", str(task_id), "-m", "e2e cli cancel"], cwd=run.sandbox)
    run.write_command("cli-task-cancel", cli_cancel)
    assert cli_cancel.returncode == 0, cli_cancel.stderr or cli_cancel.stdout
    assert "已取消" in cli_cancel.stdout

    cli_resume = _run_codepilot(["task", "resume", str(task_id)], cwd=run.sandbox)
    run.write_command("cli-task-resume", cli_resume)
    assert cli_resume.returncode == 0, cli_resume.stderr or cli_resume.stdout
    assert "backlog" in cli_resume.stdout

    status, cancel_body = _http_json(f"{base_url}/api/tasks/{task_id}/cancel", method="POST", body={})
    run.write_json("api-task-cancel", {"status": status, "body": cancel_body})
    assert status == 200
    assert cancel_body["ok"] is True
    assert db.get_task(task_id)["status"] == "cancelled"

    db.update_task(task_id, status="backlog", error_message=None)
    warning_task = db.create_task(PROJECT_NAME, f"{E2E_TITLE_PREFIX} daemon warning {run.run_id}", agent="dual")
    run.created_task_ids.append(int(warning_task["id"]))
    db.update_task(warning_task["id"], status="failed", error_message="e2e daemon start warning")
    monkeypatch.setattr(
        action_requirements,
        "_request_task_service_start",
        lambda project: (None, "e2e daemon start failed"),
    )
    status, warning_body = _http_json(f"{base_url}/api/tasks/{warning_task['id']}/retry", method="POST", body={})
    run.write_json("api-task-retry-service-warning", {"status": status, "body": warning_body})
    assert status == 200
    assert warning_body["ok"] is True
    assert warning_body["service_error"] == "e2e daemon start failed"
    assert f"codepilot daemon -p {PROJECT_NAME}" in warning_body["message"]
    assert _task_action_toast_level(status, warning_body) == "warning"
    monkeypatch.setattr(
        action_requirements,
        "_request_task_service_start",
        lambda project: ({"running": True, "started": False, "project": project}, ""),
    )

    db.update_task(task_id, status="done", completed_at=datetime.now().isoformat(timespec="seconds"))
    status, archive_body = _http_json(f"{base_url}/api/tasks/{task_id}/archive", method="POST", body={})
    run.write_json("api-task-archive", {"status": status, "body": archive_body})
    assert status == 200
    assert archive_body["ok"] is True
    assert db.get_task(task_id)["status"] == "archived"

    status, delete_body = _http_json(f"{base_url}/api/tasks/{task_id}/delete", method="POST", body={})
    run.write_json("api-task-delete", {"status": status, "body": delete_body})
    assert status == 200
    assert delete_body["ok"] is True
    assert db.get_task(task_id) is None


def _assert_batch_and_jobs(base_url: str, run: E2ERun) -> None:
    one = db.create_task(PROJECT_NAME, f"{E2E_TITLE_PREFIX} batch one {run.run_id}", agent="dual")
    two = db.create_task(PROJECT_NAME, f"{E2E_TITLE_PREFIX} batch two {run.run_id}", agent="dual")
    run.created_task_ids.extend([int(one["id"]), int(two["id"])])
    status, batch_body = _http_json(
        f"{base_url}/api/tasks/batch",
        method="POST",
        body={"action": "cancel", "task_ids": [one["id"], two["id"]]},
    )
    run.write_json("api-task-batch-cancel", {"status": status, "body": batch_body})
    assert status == 200
    assert batch_body["ok"] is True
    assert batch_body["success_count"] == 2

    job_id = webui_mod._next_job_id()
    run.created_job_ids.append(job_id)
    webui_mod._update_job(job_id, project=PROJECT_NAME, status="running", phase="e2e", title="E2E synthetic job")
    status, dashboard = _http_json(f"{base_url}/api/projects/{urllib.parse.quote(PROJECT_NAME)}")
    run.write_json("api-project-jobs", {"status": status, "body": dashboard})
    assert status == 200
    assert job_id in [int(item["id"]) for item in dashboard["jobs"]]


def _cleanup_e2e_records(run: E2ERun) -> None:
    db.init_db()
    for task_id in run.created_task_ids:
        task = db.get_task(task_id)
        if not task:
            continue
        if task.get("status") == "in_progress":
            db.update_task(task_id, status="cancelled", error_message="e2e cleanup")
        db.delete_task(task_id)
    for job_id in run.created_job_ids:
        webui_mod._UI_JOBS.pop(job_id, None)
        db.clear_service_state("webui_job", str(job_id))


@pytest.fixture()
def e2e_run(request) -> E2ERun:
    if not request.config.getoption("--run-e2e-sandbox"):
        pytest.skip("external sandbox E2E skipped; pass --run-e2e-sandbox")

    issues = _validate_sandbox()
    if issues:
        pytest.skip("external sandbox unavailable: " + "; ".join(issues))
    if shutil.which("git") is None:
        pytest.skip("git is required for sandbox snapshot/restore")

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = SANDBOX_PATH / ".codepilot" / "e2e-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run = E2ERun(
        sandbox=SANDBOX_PATH,
        run_id=run_id,
        run_dir=run_dir,
        snapshot=snapshot_tracked_files(SANDBOX_PATH),
    )
    run.write_text("git-status-before", _git_status_short(SANDBOX_PATH))

    status = _run_codepilot(["status", "-p", PROJECT_NAME, "--json"], cwd=SANDBOX_PATH)
    run.write_command("preflight-status", status)
    if status.returncode != 0:
        restore_tracked_files(SANDBOX_PATH, run.snapshot)
        pytest.skip(f"preflight status failed: {status.stderr or status.stdout}")
    payload = _parse_first_json_object(f"{status.stdout}\n{status.stderr}")
    if payload.get("ok") is not True or payload.get("data", {}).get("project") != PROJECT_NAME:
        restore_tracked_files(SANDBOX_PATH, run.snapshot)
        pytest.skip(f"preflight status returned unexpected payload: {payload}")

    try:
        yield run
    finally:
        _cleanup_e2e_records(run)
        restore_tracked_files(SANDBOX_PATH, run.snapshot)
        run.write_text("git-status-after", _git_status_short(SANDBOX_PATH))


def test_validate_sandbox_reports_missing_path(tmp_path):
    missing = tmp_path / "missing"

    issues = _validate_sandbox(missing, PROJECT_NAME)

    assert issues == [f"沙箱路径不存在：{missing}"]


def test_tracked_snapshot_restore_preserves_untracked_content(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required")
    _git_run(tmp_path, ["init"])
    tracked = tmp_path / "main.py"
    tracked.write_text("print('hello')\n", encoding="utf-8")
    add = _git_run(tmp_path, ["add", "main.py"])
    assert add.returncode == 0, add.stderr
    snapshot = snapshot_tracked_files(tmp_path)

    tracked.write_text("print('changed')\n", encoding="utf-8")
    untracked = tmp_path / ".codepilot" / "e2e-runs" / "run-1" / "artifact.txt"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("diagnostic", encoding="utf-8")

    restore_tracked_files(tmp_path, snapshot)

    assert tracked.read_text(encoding="utf-8") == "print('hello')\n"
    assert untracked.read_text(encoding="utf-8") == "diagnostic"


def test_api_assertions_catch_missing_fields_and_empty_logs():
    with pytest.raises(AssertionError):
        _assert_dashboard_payload({"selected_project": PROJECT_NAME}, project=PROJECT_NAME)
    with pytest.raises(AssertionError):
        _assert_task_detail_payload({"task": {"id": 1}}, task_id=2)
    with pytest.raises(AssertionError):
        _assert_log_payload({"offset": 0, "next_offset": 0, "size": 0, "text": "", "done": True, "path": ""})


def test_task_action_toast_semantics_treat_service_error_as_warning():
    assert _task_action_toast_level(200, {"ok": True}) == "success"
    assert _task_action_toast_level(200, {"ok": False}) == "warning"
    assert _task_action_toast_level(200, {"ok": True, "service_error": "daemon failed"}) == "warning"
    assert _task_action_toast_level(500, {"ok": False}) == "error"
    assert _task_action_toast_level(200, raised=True) == "error"


@pytest.mark.serial
@pytest.mark.slow
def test_e2e_test_codepilot_cli_webui_workflow(e2e_run: E2ERun, monkeypatch: pytest.MonkeyPatch):
    run = e2e_run
    db.init_db()

    _run_read_only_status_chain(run, monkeypatch)
    with _ui_server(run, monkeypatch) as base_url:
        task = _create_task_with_cli(run)
        task_id = int(task["id"])
        log_path = _seed_task_logs(run, task_id)
        _assert_cli_task_visibility(run, task_id)
        _assert_api_visibility(base_url, run, task_id, log_path)
        _assert_event_stream(base_url, task_id)
        _assert_task_actions(base_url, run, task_id, monkeypatch)
        _assert_batch_and_jobs(base_url, run)


@pytest.mark.serial
@pytest.mark.slow
def test_optional_browser_e2e_for_test_codepilot(
    request,
    monkeypatch: pytest.MonkeyPatch,
):
    if not request.config.getoption("--run-browser-e2e"):
        pytest.skip("browser E2E skipped; pass --run-browser-e2e")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    run = request.getfixturevalue("e2e_run")
    with _ui_server(run, monkeypatch) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                page.goto(base_url, wait_until="networkidle", timeout=15000)
                page.get_by_text(PROJECT_NAME).first.wait_for(timeout=8000)
                page.screenshot(path=str(run.run_dir / "browser-home.png"), full_page=True)
                run.write_text("browser-home.html", page.content())
            except Exception:
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(run.run_dir / "browser-failure.png"), full_page=True)
                    run.write_text("browser-failure.html", page.content())
                raise
            finally:
                browser.close()
