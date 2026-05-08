from __future__ import annotations

import pytest

from codepilot.core.config import AgentsConfig, ConfigError, tomllib


def _cfg_from_toml(body: str) -> AgentsConfig:
    return AgentsConfig.from_dict(tomllib.loads(body))


def test_scheduled_agents_parse_intervals_schedule_prompt_defaults_and_costs():
    cfg = _cfg_from_toml(
        """
[automation.scheduled_agents.todo_sweep]
agent = "codex"
interval = "10m"
prompt = "Scan TODO comments and create focused follow-up tasks."

[automation.scheduled_agents.hourly_health]
enabled = false
agent = "claude"
interval = "1h"
prompt = "Check project health signals."

[automation.scheduled_agents.daily_review]
agent = "opencode"
interval = "1d"
prompt = '''
Review failed tasks.
Summarize the next repair action.
'''
max_cost_usd = 0.25
max_daily_cost_usd = 1.5

[automation.scheduled_agents.nightly_plan]
agent = "codex"
schedule = "0 2 * * *"
prompt = "Prepare tomorrow's backlog triage."
""".strip()
    )

    scheduled = cfg.automation.scheduled_agents

    assert scheduled["todo_sweep"].enabled is True
    assert scheduled["todo_sweep"].interval == "10m"
    assert scheduled["todo_sweep"].interval_seconds == 600

    assert scheduled["hourly_health"].enabled is False
    assert scheduled["hourly_health"].interval_seconds == 3600

    assert scheduled["daily_review"].interval_seconds == 86400
    assert "Summarize the next repair action." in scheduled["daily_review"].prompt
    assert scheduled["daily_review"].max_cost_usd == 0.25
    assert scheduled["daily_review"].max_daily_cost_usd == 1.5

    assert scheduled["nightly_plan"].schedule == "0 2 * * *"
    assert scheduled["nightly_plan"].interval is None
    assert scheduled["nightly_plan"].interval_seconds is None


def test_event_agents_parse_trigger_agent_template_prompt_and_costs():
    cfg = _cfg_from_toml(
        """
[automation.event_agents.failed_task_triage]
trigger = "task.failed"
agent = "codex"
prompt = "Task {{ task.id }} failed with {{ task.error }}. Propose a repair."
max_cost_usd = 0.1
max_daily_cost_usd = 0.5
""".strip()
    )

    event_agent = cfg.automation.event_agents["failed_task_triage"]

    assert event_agent.enabled is True
    assert event_agent.trigger == "task.failed"
    assert event_agent.agent == "codex"
    assert "{{ task.error }}" in event_agent.prompt
    assert event_agent.max_cost_usd == 0.1
    assert event_agent.max_daily_cost_usd == 0.5


@pytest.mark.parametrize(
    ("body", "expected_parts"),
    [
        (
            """
[automation.scheduled_agents.bad]
agent = "codex"
prompt = "Missing cadence."
""",
            ("automation.scheduled_agents.bad", "interval", "schedule"),
        ),
        (
            """
[automation.scheduled_agents.bad]
agent = "codex"
interval = "5x"
prompt = "Bad cadence."
""",
            ("automation.scheduled_agents.bad.interval", "10m", "1h", "1d"),
        ),
        (
            """
[automation.scheduled_agents.bad]
interval = "10m"
prompt = "Missing agent."
""",
            ("automation.scheduled_agents.bad.agent",),
        ),
        (
            """
[automation.scheduled_agents.bad]
agent = "codex"
interval = "10m"
""",
            ("automation.scheduled_agents.bad.prompt",),
        ),
        (
            """
[automation.event_agents.bad]
agent = "codex"
prompt = "Missing trigger."
""",
            ("automation.event_agents.bad.trigger",),
        ),
        (
            """
[automation.event_agents.bad]
trigger = "task.failed"
prompt = "Missing agent."
""",
            ("automation.event_agents.bad.agent",),
        ),
        (
            """
[automation.event_agents.bad]
trigger = "task.failed"
agent = "codex"
""",
            ("automation.event_agents.bad.prompt",),
        ),
    ],
)
def test_automation_agent_config_errors_are_actionable(body: str, expected_parts: tuple[str, ...]):
    with pytest.raises(ConfigError) as excinfo:
        _cfg_from_toml(body.strip())

    message = str(excinfo.value)
    for part in expected_parts:
        assert part in message
