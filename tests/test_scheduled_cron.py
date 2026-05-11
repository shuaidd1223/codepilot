from __future__ import annotations

from datetime import datetime, timezone

import pytest

from codepilot.core.config import ScheduledAgentConfig
from codepilot.scheduled.triggers import (
    CronExpressionError,
    build_scheduled_agent_jobs,
    cron_due,
    interval_due,
)


UTC = timezone.utc


def test_cron_due_matches_fixed_minute_expression():
    now = datetime(2026, 5, 9, 10, 15, tzinfo=UTC)

    assert cron_due("15 * * * *", now) is True
    assert cron_due("30 * * * *", now) is False


def test_cron_due_matches_hourly_expression():
    assert cron_due("0 * * * *", datetime(2026, 5, 9, 10, 0, tzinfo=UTC)) is True
    assert cron_due("0 * * * *", datetime(2026, 5, 9, 10, 1, tzinfo=UTC)) is False


def test_cron_due_matches_daily_expression():
    assert cron_due("0 9 * * *", datetime(2026, 5, 9, 9, 0, tzinfo=UTC)) is True
    assert cron_due("0 9 * * *", datetime(2026, 5, 9, 10, 0, tzinfo=UTC)) is False


@pytest.mark.parametrize("expr", ["", "* * * *", "*/5 * * * *", "61 * * * *", "0 24 * * *"])
def test_cron_due_rejects_unsupported_or_invalid_expressions(expr: str):
    with pytest.raises(CronExpressionError):
        cron_due(expr, datetime(2026, 5, 9, 9, 0, tzinfo=UTC))


def test_interval_due_uses_injected_clock_and_last_run():
    now = datetime(2026, 5, 9, 10, 0, tzinfo=UTC)

    assert interval_due(600, last_run_at=None, now=now) is True
    assert interval_due(600, last_run_at=datetime(2026, 5, 9, 9, 49, tzinfo=UTC), now=now) is True
    assert interval_due(600, last_run_at=datetime(2026, 5, 9, 9, 55, tzinfo=UTC), now=now) is False


def test_build_scheduled_agent_jobs_returns_due_agent_job_payloads_only():
    now = datetime(2026, 5, 9, 9, 0, tzinfo=UTC)
    configs = {
        "hourly_health": ScheduledAgentConfig(
            agent="codex",
            interval="1h",
            interval_seconds=3600,
            prompt="Check project health.",
        ),
        "daily_backlog": ScheduledAgentConfig(
            agent="claude",
            schedule="0 9 * * *",
            prompt="Review backlog.",
            max_cost_usd=0.3,
        ),
        "disabled": ScheduledAgentConfig(
            enabled=False,
            agent="codex",
            schedule="0 9 * * *",
            prompt="Should not run.",
        ),
    }

    jobs = build_scheduled_agent_jobs(
        configs,
        project="demo",
        now=now,
        last_run_at={"hourly_health": datetime(2026, 5, 9, 7, 59, tzinfo=UTC)},
    )

    assert [job["name"] for job in jobs] == ["hourly_health", "daily_backlog"]
    assert jobs[0]["type"] == "agent_job"
    assert jobs[0]["trigger"]["type"] == "interval"
    assert jobs[0]["agent"] == "codex"
    assert jobs[0]["prompt"] == "Check project health."
    assert jobs[1]["trigger"]["type"] == "schedule"
    assert jobs[1]["trigger"]["schedule"] == "0 9 * * *"
    assert jobs[1]["max_cost_usd"] == 0.3


def test_build_scheduled_agent_jobs_dedupes_cron_slot_with_last_run():
    now = datetime(2026, 5, 9, 9, 0, tzinfo=UTC)
    configs = {
        "daily_backlog": ScheduledAgentConfig(
            agent="claude",
            schedule="0 9 * * *",
            prompt="Review backlog.",
        ),
    }

    jobs = build_scheduled_agent_jobs(
        configs,
        project="demo",
        now=now,
        last_run_at={"daily_backlog": datetime(2026, 5, 9, 9, 0, 30, tzinfo=UTC)},
    )

    assert jobs == []
