"""Pure trigger helpers for scheduled and event-driven automation agents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from codepilot.core.config import EventAgentConfig, ScheduledAgentConfig
from codepilot.scheduled.templates import render_prompt_template


class CronExpressionError(ValueError):
    """Raised when a cron-like expression is outside the supported subset."""


@dataclass(frozen=True)
class _CronField:
    value: int | None
    minimum: int
    maximum: int
    name: str

    def matches(self, candidate: int) -> bool:
        return self.value is None or self.value == candidate


def _parse_cron_field(raw: str, *, minimum: int, maximum: int, name: str) -> _CronField:
    token = str(raw or "").strip()
    if token == "*":
        return _CronField(None, minimum, maximum, name)
    if not token.isdigit():
        raise CronExpressionError(
            f"Unsupported cron field {name!r}: {token!r}. "
            "Only '*' or a fixed integer is supported."
        )
    value = int(token)
    if value < minimum or value > maximum:
        raise CronExpressionError(
            f"Invalid cron field {name!r}: {value}. Expected {minimum}-{maximum}."
        )
    return _CronField(value, minimum, maximum, name)


def _parse_cron(expression: str) -> tuple[_CronField, _CronField, _CronField, _CronField, _CronField]:
    parts = str(expression or "").split()
    if len(parts) != 5:
        raise CronExpressionError(
            "Cron schedule must contain exactly five fields: minute hour day month weekday."
        )
    return (
        _parse_cron_field(parts[0], minimum=0, maximum=59, name="minute"),
        _parse_cron_field(parts[1], minimum=0, maximum=23, name="hour"),
        _parse_cron_field(parts[2], minimum=1, maximum=31, name="day"),
        _parse_cron_field(parts[3], minimum=1, maximum=12, name="month"),
        _parse_cron_field(parts[4], minimum=0, maximum=7, name="weekday"),
    )


def cron_due(expression: str, now: datetime) -> bool:
    """Return whether ``now`` matches the supported five-field cron subset.

    Supported fields are ``*`` and fixed integers only, which covers hourly
    forms like ``0 * * * *`` and daily forms like ``0 9 * * *``. Step, range,
    list, named month and named weekday expressions are rejected explicitly.
    Weekday accepts 0 or 7 as Sunday.
    """

    minute, hour, day, month, weekday = _parse_cron(expression)
    cron_weekday = (now.weekday() + 1) % 7
    weekday_matches = weekday.matches(cron_weekday) or (
        weekday.value == 7 and cron_weekday == 0
    )
    return (
        minute.matches(now.minute)
        and hour.matches(now.hour)
        and day.matches(now.day)
        and month.matches(now.month)
        and weekday_matches
    )


def interval_due(interval_seconds: int, *, last_run_at: datetime | None, now: datetime) -> bool:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than 0.")
    if last_run_at is None:
        return True
    return (now - last_run_at).total_seconds() >= interval_seconds


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("type") or event.get("event_type") or "").strip()


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _project_from_event(event: Mapping[str, Any], fallback: str | None) -> str:
    return str(fallback or event.get("project") or "").strip()


def _job_payload(
    *,
    name: str,
    project: str,
    agent: str,
    prompt: str,
    trigger: dict[str, Any],
    source_event: Mapping[str, Any] | None = None,
    max_cost_usd: float | None = None,
    max_daily_cost_usd: float | None = None,
) -> dict[str, Any]:
    job = {
        "type": "agent_job",
        "name": name,
        "project": project,
        "agent": agent,
        "prompt": prompt,
        "trigger": trigger,
        "max_cost_usd": max_cost_usd,
        "max_daily_cost_usd": max_daily_cost_usd,
    }
    if source_event is not None:
        job["source_event"] = dict(source_event)
    return job


def build_event_agent_jobs(
    event_agents: Mapping[str, EventAgentConfig],
    event: Mapping[str, Any],
    *,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Build runner-ready agent jobs for enabled event agents matching ``event``."""

    event_type = _event_type(event)
    payload = _event_payload(event)
    context = {"event": dict(event), "payload": payload, **payload}
    jobs: list[dict[str, Any]] = []
    for name, config in event_agents.items():
        if not config.enabled or config.trigger != event_type:
            continue
        prompt = render_prompt_template(config.prompt, context)
        jobs.append(
            _job_payload(
                name=name,
                project=_project_from_event(event, project),
                agent=config.agent,
                prompt=prompt,
                trigger={"type": "event", "event_type": event_type},
                source_event=event,
                max_cost_usd=config.max_cost_usd,
                max_daily_cost_usd=config.max_daily_cost_usd,
            )
        )
    return jobs


def build_scheduled_agent_jobs(
    scheduled_agents: Mapping[str, ScheduledAgentConfig],
    *,
    project: str,
    now: datetime,
    last_run_at: Mapping[str, datetime] | None = None,
) -> list[dict[str, Any]]:
    """Build runner-ready agent jobs for enabled interval or cron agents due at ``now``."""

    last_runs = dict(last_run_at or {})
    jobs: list[dict[str, Any]] = []
    for name, config in scheduled_agents.items():
        if not config.enabled:
            continue
        trigger: dict[str, Any] | None = None
        if config.interval_seconds is not None and interval_due(
            config.interval_seconds,
            last_run_at=last_runs.get(name),
            now=now,
        ):
            trigger = {
                "type": "interval",
                "interval": config.interval,
                "interval_seconds": config.interval_seconds,
            }
        elif config.schedule and cron_due(config.schedule, now):
            trigger = {"type": "schedule", "schedule": config.schedule}
        if trigger is None:
            continue
        jobs.append(
            _job_payload(
                name=name,
                project=project,
                agent=config.agent,
                prompt=render_prompt_template(
                    config.prompt,
                    {"now": now.isoformat(), "project": project},
                ),
                trigger=trigger,
                max_cost_usd=config.max_cost_usd,
                max_daily_cost_usd=config.max_daily_cost_usd,
            )
        )
    return jobs
