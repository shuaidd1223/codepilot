"""Tests for the progress bus, ETA computation, and Web UI interaction helpers."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from codepilot.storage import database as db
from codepilot.core import progress_bus
from codepilot.webapp import server as webui_mod


# ─── progress_bus ─────────────────────────────────────────────────────────


def test_progress_bus_delivers_events_to_all_subscribers():
    progress_bus.clear_subscribers_for_tests()
    received_a: list[dict] = []
    received_b: list[dict] = []
    ta = progress_bus.subscribe(received_a.append)
    tb = progress_bus.subscribe(received_b.append)

    progress_bus.emit(stage="planner", message="hi", task_id=7, extra={"round": 1})

    assert len(received_a) == 1
    assert received_a[0]["stage"] == "planner"
    assert received_a[0]["task_id"] == 7
    assert received_a[0]["extra"] == {"round": 1}
    assert received_a == received_b

    progress_bus.unsubscribe(ta)
    progress_bus.unsubscribe(tb)


def test_progress_bus_unsubscribe_stops_delivery():
    progress_bus.clear_subscribers_for_tests()
    received: list[dict] = []
    t = progress_bus.subscribe(received.append)
    progress_bus.emit(stage="x", message="first")
    progress_bus.unsubscribe(t)
    progress_bus.emit(stage="x", message="second")
    assert len(received) == 1
    assert received[0]["message"] == "first"


def test_progress_bus_swallows_subscriber_exceptions():
    progress_bus.clear_subscribers_for_tests()
    calls: list[str] = []

    def _boom(event):
        raise RuntimeError("bad subscriber")

    def _good(event):
        calls.append(event["message"])

    tb = progress_bus.subscribe(_boom)
    tg = progress_bus.subscribe(_good)
    progress_bus.emit(stage="x", message="survives")
    assert calls == ["survives"]
    progress_bus.unsubscribe(tb)
    progress_bus.unsubscribe(tg)


def test_progress_bus_subscription_context_manager():
    progress_bus.clear_subscribers_for_tests()
    received: list[dict] = []
    with progress_bus.subscription(received.append):
        progress_bus.emit(stage="stage", message="hello")
    progress_bus.emit(stage="stage", message="nope")
    assert len(received) == 1
    assert received[0]["message"] == "hello"


def test_progress_bus_assigns_monotonic_event_ids():
    progress_bus.clear_subscribers_for_tests()
    progress_bus.emit(stage="planner", message="a")
    progress_bus.emit(stage="planner", message="b")
    events = progress_bus.events_since(0)
    assert [event["id"] for event in events] == [1, 2]


def test_progress_bus_preserves_explicit_event_type_and_infers_error_type():
    progress_bus.clear_subscribers_for_tests()
    progress_bus.emit(stage="planner", message="start", event_type="phase_start")
    progress_bus.emit(stage="planner", message="boom", level="error")

    events = progress_bus.events_since(0)
    assert events[0]["type"] == "phase_start"
    assert events[1]["type"] == "error"


def test_progress_bus_subscribe_with_backlog_replays_tail_since_event_id():
    progress_bus.clear_subscribers_for_tests()
    progress_bus.emit(stage="planner", message="first")
    progress_bus.emit(stage="builder", message="second")
    first_id = progress_bus.events_since(0)[0]["id"]

    live: list[dict] = []
    token, backlog = progress_bus.subscribe_with_backlog(live.append, after_id=first_id)
    try:
        assert [item["message"] for item in backlog] == ["second"]
        progress_bus.emit(stage="reviewer", message="third")
        assert [item["message"] for item in live] == ["third"]
    finally:
        progress_bus.unsubscribe(token)


def test_progress_bus_llm_context_scopes_and_restores_metadata():
    progress_bus.clear_subscribers_for_tests()

    assert progress_bus.current_llm_context() == {}
    assert progress_bus.has_subscribers() is False

    with progress_bus.subscription(lambda _event: None):
        assert progress_bus.has_subscribers() is True
        with progress_bus.llm_context(task_id=9, stage="planner", label="意图分类"):
            assert progress_bus.current_llm_context() == {
                "task_id": 9,
                "stage": "planner",
                "label": "意图分类",
            }
            with progress_bus.llm_context(stage="clarify"):
                assert progress_bus.current_llm_context() == {
                    "task_id": 9,
                    "stage": "clarify",
                    "label": "意图分类",
                }

    assert progress_bus.has_subscribers() is False
    assert progress_bus.current_llm_context() == {}


# ─── executor emits events on the bus ─────────────────────────────────────


def test_builtin_executor_emits_round_events(tmp_path, monkeypatch):
    """Builder/Reviewer rounds should surface as progress events with round metadata."""
    from codepilot.commands import run as run_mod

    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    (proj_root / "README.md").write_text("# x", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    monkeypatch.setattr(run_mod, "_builtin_preflight_error", lambda *a, **kw: "")
    monkeypatch.setattr(run_mod, "_git_auto_commit", lambda *a, **kw: "deadbee")
    monkeypatch.setattr(run_mod, "_write_task_log", lambda *a, **kw: None)
    monkeypatch.setattr(run_mod, "_builtin_runtime_dir", lambda project: runtime_dir)

    phases = iter([
        ("codex", 0, "built"),
        ("codex-review", 0, "ok\nVERDICT: PASS"),
    ])
    monkeypatch.setattr(run_mod, "_run_builtin_phase", lambda **kw: next(phases))

    progress_bus.clear_subscribers_for_tests()
    events: list[dict] = []
    with progress_bus.subscription(events.append):
        run_mod._run_builtin_executor(
            {"id": 101, "title": "demo", "agent": "dual", "content": ""},
            {"name": "demo", "path": str(proj_root), "config_file": None},
            tmp_path / "task.md",
            auto_commit=False,
            max_review_rounds=2,
        )

    # We should see at least: builder-start, reviewer-start, reviewer PASS.
    stages = [e["stage"] for e in events]
    assert "builder" in stages
    assert "reviewer" in stages
    start_events = [e for e in events if e.get("type") == "phase_start"]
    assert any(e["stage"] == "builder" for e in start_events)
    assert any(e["stage"] == "reviewer" for e in start_events)
    assert any(e["stage"] == "builder" and e.get("type") == "phase_end" for e in events)
    pass_events = [e for e in events if e["stage"] == "reviewer" and "PASS" in (e["message"] or "")]
    assert pass_events
    assert pass_events[0]["type"] == "phase_end"
    assert pass_events[0]["extra"].get("review_verdict") is True
    assert pass_events[0]["extra"].get("verdict") == "pass"
    assert pass_events[0]["extra"].get("blockers") == []
    assert pass_events[0]["extra"].get("blocker_count") == 0


def test_builtin_executor_emits_structured_reviewer_blockers(tmp_path, monkeypatch):
    """FAIL verdict events should carry structured blockers on the progress bus."""
    from codepilot.commands import run as run_mod

    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    (proj_root / "README.md").write_text("# x", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    monkeypatch.setattr(run_mod, "_builtin_preflight_error", lambda *a, **kw: "")
    monkeypatch.setattr(run_mod, "_git_auto_commit", lambda *a, **kw: "deadbee")
    monkeypatch.setattr(run_mod, "_write_task_log", lambda *a, **kw: None)
    monkeypatch.setattr(run_mod, "_builtin_runtime_dir", lambda project: runtime_dir)

    fail_review = (
        "发现问题\n"
        "VERDICT: FAIL\n"
        "```json\n"
        '{"verdict":"fail","blockers":["补上 --json 分支","修复超时处理"],"advisory":["文案再压缩"]}\n'
        "```"
    )
    phases = iter([
        ("codex", 0, "built"),
        ("codex-review", 0, fail_review),
        ("codex", 0, "rebuilt"),
        ("codex-review", 0, "ok\nVERDICT: PASS"),
    ])
    monkeypatch.setattr(run_mod, "_run_builtin_phase", lambda **kw: next(phases))

    progress_bus.clear_subscribers_for_tests()
    events: list[dict] = []
    with progress_bus.subscription(events.append):
        run_mod._run_builtin_executor(
            {"id": 102, "title": "demo", "agent": "dual", "content": ""},
            {"name": "demo", "path": str(proj_root), "config_file": None},
            tmp_path / "task.md",
            auto_commit=False,
            max_review_rounds=2,
        )

    fail_events = [
        e for e in events
        if e["stage"] == "reviewer" and (e.get("extra") or {}).get("review_verdict") is True
        and (e.get("extra") or {}).get("verdict") == "fail"
    ]
    assert fail_events
    assert fail_events[0]["type"] == "phase_end"
    extra = fail_events[0]["extra"]
    assert extra["blockers"] == ["补上 --json 分支", "修复超时处理"]
    assert extra["blocker_count"] == 2
    assert extra["advisory"] == ["文案再压缩"]
    assert extra["advisory_count"] == 1
    assert extra["source"] == "json"


# ─── DB ETA ────────────────────────────────────────────────────────────────


def test_compute_agent_eta_seconds_uses_median_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("eta-demo", str(tmp_path))

    base = datetime(2026, 4, 19, 10, 0, 0)
    # Seed 5 done tasks with varied durations (60s, 120s, 180s, 240s, 300s).
    # Median = 180s.
    for i, delta in enumerate([60, 120, 180, 240, 300]):
        task = db.create_task("eta-demo", f"seed {i}", agent="codex")
        started = base + timedelta(minutes=i)
        finished = started + timedelta(seconds=delta)
        db.update_task(
            task["id"],
            status="done",
            started_at=started.isoformat(timespec="seconds"),
            completed_at=finished.isoformat(timespec="seconds"),
        )

    eta = db.compute_agent_eta_seconds("eta-demo", "codex")
    assert eta is not None
    # Median with 5 samples → the middle one → 180s.
    assert 170 <= eta <= 190


def test_compute_agent_eta_seconds_returns_none_without_enough_history(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("eta-thin", str(tmp_path))
    # Only 2 done tasks — not enough.
    for i in range(2):
        task = db.create_task("eta-thin", f"seed {i}", agent="codex")
        db.update_task(
            task["id"],
            status="done",
            started_at="2026-04-19T10:00:00",
            completed_at="2026-04-19T10:01:00",
        )
    assert db.compute_agent_eta_seconds("eta-thin", "codex") is None


def test_compute_agent_eta_seconds_filters_by_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("eta-filter", str(tmp_path))

    for i in range(4):
        t = db.create_task("eta-filter", f"codex {i}", agent="codex")
        db.update_task(
            t["id"], status="done",
            started_at="2026-04-19T10:00:00", completed_at="2026-04-19T10:00:30",
        )
    for i in range(4):
        t = db.create_task("eta-filter", f"claude {i}", agent="claude")
        db.update_task(
            t["id"], status="done",
            started_at="2026-04-19T10:00:00", completed_at="2026-04-19T10:05:00",
        )

    codex_eta = db.compute_agent_eta_seconds("eta-filter", "codex")
    claude_eta = db.compute_agent_eta_seconds("eta-filter", "claude")
    assert codex_eta == 30
    assert claude_eta == 300


# ─── webui payload exposes eta_seconds ─────────────────────────────────────


def test_task_payload_includes_eta_for_backlog(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("p-eta", str(tmp_path))

    # Seed done tasks so there's history.
    for _ in range(4):
        t = db.create_task("p-eta", "seed", agent="codex")
        db.update_task(
            t["id"], status="done",
            started_at="2026-04-19T10:00:00", completed_at="2026-04-19T10:02:00",
        )

    # New backlog task — payload should carry eta_seconds.
    target = db.create_task("p-eta", "pending", agent="codex")
    payload = webui_mod._task_payload(db.get_task(target["id"]))
    assert payload["eta_seconds"] is not None
    assert payload["eta_seconds"] >= 60


def test_task_payload_skips_eta_for_done(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("p-eta2", str(tmp_path))
    t = db.create_task("p-eta2", "finished", agent="codex")
    db.update_task(
        t["id"], status="done",
        started_at="2026-04-19T10:00:00", completed_at="2026-04-19T10:01:30",
    )
    payload = webui_mod._task_payload(db.get_task(t["id"]))
    assert payload["eta_seconds"] is None


# ─── split action ─────────────────────────────────────────────────────────


def test_split_task_action_cancels_original_and_spawns_job(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    (tmp_path / "project").mkdir()
    db.register_project("splitdemo", str(tmp_path / "project"))
    webui_mod._UI_JOBS.clear()
    webui_mod._UI_EVENTS.clear()
    webui_mod._UI_JOB_SEQ = 0

    task = db.create_task("splitdemo", "太大的任务", agent="codex")
    # Stub submit_requirement_action so we don't spin up a real planner.
    called = {}

    def fake_submit(project, title, **kwargs):
        called["title"] = title
        called["kwargs"] = kwargs
        return {"ok": True, "message": "queued", "job": {"id": 99, "task_ids": []}}

    monkeypatch.setattr(
        "codepilot.webapp.actions.submit_requirement_action", fake_submit
    )

    result = webui_mod.split_task_action(task["id"])
    assert result["ok"] is True
    assert result["intent"] == "split"
    assert result["original_task_id"] == task["id"]

    reloaded = db.get_task(task["id"])
    assert reloaded["status"] == "cancelled"
    assert "太大的任务" in called["title"]


# ─── webhook desktop notification is best-effort ───────────────────────────


def test_desktop_notification_never_raises(monkeypatch):
    """Even if the OS tooling is missing, the helper must not propagate errors."""
    from codepilot.webapp import webhook

    # Force 'linux' path + drop notify-send to simulate an absent tool.
    import platform as _platform
    import shutil as _shutil
    monkeypatch.setattr(_platform, "system", lambda: "Linux")
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    # Should return False but not raise.
    assert webhook._send_desktop_notification(title="t", body="b") is False


def test_desktop_notification_honors_env_opt_out(monkeypatch):
    from codepilot.webapp import webhook
    monkeypatch.setenv("CODEPILOT_DESKTOP_NOTIFY", "0")
    assert webhook._send_desktop_notification(title="t", body="b") is False


def test_windows_desktop_notification_falls_back_to_msg(monkeypatch):
    from codepilot.webapp import webhook

    import platform as _platform
    import shutil as _shutil
    import subprocess as _subprocess

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    calls = []

    monkeypatch.setenv("CODEPILOT_DESKTOP_NOTIFY", "1")
    monkeypatch.setattr(_platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        _shutil,
        "which",
        lambda name: "msg.exe" if name in {"msg.exe", "msg"} else None,
    )

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "powershell.exe":
            return Result(1)
        return Result(0)

    monkeypatch.setattr(_subprocess, "run", fake_run)

    assert webhook._send_desktop_notification(title="t", body="b") is True
    assert calls[0][0] == "powershell.exe"
    assert "ToastGeneric" in calls[0][-1]
    assert calls[1][0] == "msg.exe"

