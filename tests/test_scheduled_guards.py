from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from codepilot.scheduled.runner import run_agent_job


@dataclass
class Completed:
    args: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


UTC = timezone.utc


def _job(**overrides: Any) -> dict[str, Any]:
    job = {
        "type": "agent_job",
        "name": "hourly_health",
        "project": "demo",
        "agent": "codex",
        "prompt": "Check project health.",
        "trigger": {"type": "interval", "interval": "1h"},
    }
    job.update(overrides)
    return job


def _fake_run_factory(calls: list[list[str]], *, total_tokens: int, cost: float):
    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        payload = json.dumps(
            {
                "tool_calls": [{"name": "Read"}],
                "usage": {"input_tokens": total_tokens - 1, "output_tokens": 1, "total_tokens": total_tokens},
                "cost": cost,
            }
        )
        return Completed(args=list(args), stdout=f"done\n{payload}\n")

    return fake_run


def _audit_records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / ".codepilot" / "scheduled" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_max_tokens_per_call_guard_records_audit_and_notification(tmp_path: Path):
    calls: list[list[str]] = []

    result = run_agent_job(
        _job(max_tokens_per_call=10),
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=11, cost=0.02),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 9, 1, 0, tzinfo=UTC),
    )

    assert len(calls) == 1
    assert result.guard_status == "tripped"
    assert result.guard_reason == "max_tokens_per_call"
    assert result.notification["type"] == "scheduled.agent.guard_triggered"
    assert result.notification["payload"]["limit"] == 10
    records = _audit_records(tmp_path)
    assert records[0]["guard"]["reason"] == "max_tokens_per_call"
    assert records[0]["token_usage"]["total_tokens"] == 11


def test_max_cost_per_day_accumulates_disables_and_skips_later_runs(tmp_path: Path):
    calls: list[list[str]] = []
    job = _job(max_daily_cost_usd=0.1)

    first = run_agent_job(
        job,
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=8, cost=0.07),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 9, 1, 0, tzinfo=UTC),
    )
    second = run_agent_job(
        job,
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=8, cost=0.05),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 9, 2, 0, tzinfo=UTC),
    )
    third = run_agent_job(
        job,
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=8, cost=0.01),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 9, 3, 0, tzinfo=UTC),
    )

    assert len(calls) == 2
    assert first.guard_status == "ok"
    assert second.guard_status == "tripped"
    assert second.guard_reason == "max_cost_per_day"
    assert second.notification["payload"]["daily_cost_usd"] == 0.12
    assert third.guard_status == "skipped"
    assert third.guard_reason == "disabled"
    assert third.exit_code is None

    state = json.loads((tmp_path / ".codepilot" / "scheduled" / "guards.json").read_text(encoding="utf-8"))
    assert state["agents"]["demo:hourly_health"]["disabled"] is True
    assert state["daily_usage"]["2026-05-09"]["demo:hourly_health"]["cost_usd"] == 0.12
    records = _audit_records(tmp_path)
    assert [record["guard"]["status"] for record in records] == ["ok", "tripped", "skipped"]


def test_daily_cost_disablement_resets_on_next_day(tmp_path: Path):
    calls: list[list[str]] = []
    job = _job(max_daily_cost_usd=0.1)

    run_agent_job(
        job,
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=8, cost=0.12),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 9, 23, 0, tzinfo=UTC),
    )
    next_day = run_agent_job(
        job,
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=8, cost=0.01),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 10, 1, 0, tzinfo=UTC),
    )

    assert len(calls) == 2
    assert next_day.guard_status == "ok"
    state = json.loads((tmp_path / ".codepilot" / "scheduled" / "guards.json").read_text(encoding="utf-8"))
    assert state["agents"].get("demo:hourly_health", {}).get("disabled") is not True


def test_guarded_agent_under_limits_runs_without_notification(tmp_path: Path):
    calls: list[list[str]] = []

    result = run_agent_job(
        _job(max_tokens_per_call=20, max_daily_cost_usd=0.1),
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=10, cost=0.03),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 9, 1, 0, tzinfo=UTC),
    )

    assert len(calls) == 1
    assert result.guard_status == "ok"
    assert result.guard_reason == ""
    assert result.notification == {}


def test_agent_job_chain_depth_five_is_allowed(tmp_path: Path):
    calls: list[list[str]] = []

    result = run_agent_job(
        _job(agent_chain=["root", "triage", "repair", "review"]),
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=5, cost=0.01),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 9, 1, 0, tzinfo=UTC),
    )

    assert len(calls) == 1
    assert result.guard_status == "ok"
    assert result.agent_chain == ["root", "triage", "repair", "review", "demo:hourly_health"]


def test_agent_job_chain_depth_six_trips_loop_circuit_breaker(tmp_path: Path):
    calls: list[list[str]] = []

    result = run_agent_job(
        _job(agent_chain=["root", "triage", "repair", "review", "notify"]),
        project_root=tmp_path,
        subprocess_run=_fake_run_factory(calls, total_tokens=5, cost=0.01),
        commands={"codex": "codex-bin"},
        now=datetime(2026, 5, 9, 1, 0, tzinfo=UTC),
    )

    assert calls == []
    assert result.guard_status == "skipped"
    assert result.guard_reason == "loop_circuit_breaker"
    assert result.notification["payload"]["depth"] == 6
    assert result.agent_chain == [
        "root",
        "triage",
        "repair",
        "review",
        "notify",
        "demo:hourly_health",
    ]
    records = _audit_records(tmp_path)
    assert records[0]["guard"]["reason"] == "loop_circuit_breaker"
    assert records[0]["agent_chain"] == result.agent_chain
