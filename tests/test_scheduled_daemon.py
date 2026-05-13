from __future__ import annotations

import json
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codepilot.commands import daemon as daemon_cmd
from codepilot.core.config import AgentsConfig, tomllib
from codepilot.core.event_plugins import build_event, dispatch_event_to_sinks
from codepilot.storage import database as db
from codepilot.scheduled.daemon import (
    ensure_daemon_event_sink,
    run_event_agent_jobs,
    run_scheduled_agent_jobs,
)


UTC = timezone.utc


@dataclass
class Completed:
    args: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _cfg_from_toml(body: str) -> AgentsConfig:
    return AgentsConfig.from_dict(tomllib.loads(body))


def _fake_run_factory(calls: list[list[str]], *, cost: float = 0.01):
    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        payload = json.dumps(
            {
                "tool_calls": [{"name": "Read"}],
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                "cost": cost,
            }
        )
        return Completed(args=list(args), stdout=f"ok\n{payload}\n")

    return fake_run


def _agent_config() -> AgentsConfig:
    return _cfg_from_toml(
        """
[agents.commands]
codex = "codex-bin"

[automation.scheduled_agents.hourly_health]
agent = "codex"
interval = "1h"
prompt = "Check {{ project }} at {{ now }}."

[automation.event_agents.failed_task_triage]
trigger = "task.failed"
agent = "codex"
prompt = "Task {{ task_id }} failed: {{ error_message }}"
max_daily_cost_usd = 1.0
""".strip()
    )


def test_run_scheduled_agent_jobs_builds_due_jobs_and_uses_runner_dry_run(tmp_path: Path):
    calls: list[list[str]] = []
    now = datetime(2026, 5, 9, 9, 0, tzinfo=UTC)

    summary = run_scheduled_agent_jobs(
        {"name": "demo", "path": str(tmp_path)},
        _agent_config(),
        now=now,
        dry_run=True,
        subprocess_run=_fake_run_factory(calls),
    )

    assert calls == []
    assert summary["job_count"] == 1
    assert summary["jobs"][0]["name"] == "hourly_health"
    assert summary["results"][0].dry_run is True
    assert summary["results"][0].command[0] == "codex-bin"
    audit = json.loads((tmp_path / ".codepilot" / "scheduled" / "audit.jsonl").read_text().splitlines()[0])
    assert audit["job_name"] == "hourly_health"
    assert audit["trigger"]["type"] == "interval"


def test_run_event_agent_jobs_consumes_task_updated_sink_as_task_failed(tmp_path: Path):
    calls: list[list[str]] = []
    ensure_daemon_event_sink(tmp_path)
    event = build_event(
        "demo",
        "task.updated",
        source="codepilot.task",
        payload={
            "task_id": 42,
            "status": "failed",
            "error_message": "pytest failed",
        },
        event_id_prefix="task-42",
    )
    dispatch_event_to_sinks(tmp_path, event)

    summary = run_event_agent_jobs(
        {"name": "demo", "path": str(tmp_path)},
        _agent_config(),
        dry_run=False,
        subprocess_run=_fake_run_factory(calls),
    )

    assert calls == [["codex-bin", "exec", "--ephemeral", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "Task 42 failed: pytest failed"]]
    assert summary["event_count"] == 1
    assert summary["job_count"] == 1
    assert summary["jobs"][0]["trigger"]["event_type"] == "task.failed"
    assert summary["results"][0].exit_code == 0


def test_event_agent_guards_prevent_real_execution_for_disabled_cost_and_loop(tmp_path: Path):
    calls: list[list[str]] = []
    cfg = _cfg_from_toml(
        """
[agents.commands]
codex = "codex-bin"

[automation.event_agents.failed_task_triage]
trigger = "task.failed"
agent = "codex"
prompt = "Task {{ task_id }} failed."
max_daily_cost_usd = 0.01
""".strip()
    )
    event = {
        "type": "task.failed",
        "project": "demo",
        "payload": {"task_id": 7, "chain": ["root", "a", "b", "c", "d"]},
    }

    loop_summary = run_event_agent_jobs(
        {"name": "demo", "path": str(tmp_path)},
        cfg,
        events=[event],
        dry_run=False,
        subprocess_run=_fake_run_factory(calls, cost=0.02),
        now=datetime(2026, 5, 9, 1, 0, tzinfo=UTC),
    )
    first_summary = run_event_agent_jobs(
        {"name": "demo", "path": str(tmp_path)},
        cfg,
        events=[{"type": "task.failed", "project": "demo", "payload": {"task_id": 8}}],
        dry_run=False,
        subprocess_run=_fake_run_factory(calls, cost=0.02),
        now=datetime(2026, 5, 9, 2, 0, tzinfo=UTC),
    )
    disabled_summary = run_event_agent_jobs(
        {"name": "demo", "path": str(tmp_path)},
        cfg,
        events=[{"type": "task.failed", "project": "demo", "payload": {"task_id": 9}}],
        dry_run=False,
        subprocess_run=_fake_run_factory(calls, cost=0.02),
        now=datetime(2026, 5, 9, 3, 0, tzinfo=UTC),
    )

    assert calls == [["codex-bin", "exec", "--ephemeral", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "Task 8 failed."]]
    assert loop_summary["results"][0].guard_reason == "loop_circuit_breaker"
    assert first_summary["results"][0].guard_reason == "max_cost_per_day"
    assert disabled_summary["results"][0].guard_reason == "disabled"


def test_daemon_loop_invokes_scheduled_event_agent_tick_even_when_backlog_is_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    project_root = tmp_path / "project"
    project_root.mkdir()
    db.register_project("demo", str(project_root))

    ticks: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(daemon_cmd, "_start_heartbeat_thread", lambda project: types.SimpleNamespace(set=lambda: None))
    monkeypatch.setattr(daemon_cmd, "_tick_heartbeat", lambda project: None)
    monkeypatch.setattr(daemon_cmd, "_stop_requested", lambda project: False)
    monkeypatch.setattr(daemon_cmd, "reap_stalled_tasks", lambda project: [])
    monkeypatch.setattr(daemon_cmd, "_run_agent_daemon_tick", lambda project, *, verbose: ticks.append((project, verbose)))
    monkeypatch.setattr(daemon_cmd, "_get_combined_stats", lambda project: {"backlog": 0, "in_progress": 0})
    monkeypatch.setattr(daemon_cmd, "_sleep_or_stop", lambda project, interval: True)

    daemon_cmd._run_loop("demo", interval=1, verbose=True, shell="auto", executor="builtin", auto_commit=False)

    assert ticks == [("demo", True)]
