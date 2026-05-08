from __future__ import annotations

from codepilot.core.config import EventAgentConfig
from codepilot.scheduled.templates import render_prompt_template
from codepilot.scheduled.triggers import build_event_agent_jobs


def test_render_prompt_template_supports_top_level_and_dotted_fields():
    text = render_prompt_template(
        "Task {{ task_id }} / {{ task.title }} failed: {{ task.error }}.",
        {
            "task_id": 42,
            "task": {"title": "Fix scheduler", "error": "boom"},
        },
    )

    assert text == "Task 42 / Fix scheduler failed: boom."


def test_render_prompt_template_keeps_auditable_marker_for_missing_fields():
    text = render_prompt_template(
        "Task {{ task_id }} failed: {{ task.error }} {{ missing.value }}.",
        {"task_id": 42, "task": {"title": "Fix scheduler"}},
    )

    assert text == "Task 42 failed: [[missing:task.error]] [[missing:missing.value]]."


def test_build_event_agent_jobs_matches_task_failed_task_completed_commit_and_feishu_message():
    configs = {
        "failed_task_triage": EventAgentConfig(
            trigger="task.failed",
            agent="codex",
            prompt="Task {{ task_id }} failed: {{ task.title }} / {{ error }}",
        ),
        "completed_summary": EventAgentConfig(
            trigger="task.completed",
            agent="claude",
            prompt="Summarize completed task {{ task_id }}.",
        ),
        "commit_review": EventAgentConfig(
            trigger="commit",
            agent="codex",
            prompt="Review commit {{ commit.sha }} by {{ author }}.",
        ),
        "feishu_router": EventAgentConfig(
            trigger="feishu_message",
            agent="opencode",
            prompt="Handle Feishu message {{ text }}.",
        ),
    }

    failed_jobs = build_event_agent_jobs(
        configs,
        {
            "type": "task.failed",
            "project": "demo",
            "payload": {"task_id": 7, "task": {"title": "Repair CLI"}, "error": "pytest failed"},
        },
    )
    completed_jobs = build_event_agent_jobs(
        configs,
        {"type": "task.completed", "project": "demo", "payload": {"task_id": 8}},
    )
    commit_jobs = build_event_agent_jobs(
        configs,
        {
            "type": "commit",
            "project": "demo",
            "payload": {"commit": {"sha": "abc123"}, "author": "dev"},
        },
    )
    feishu_jobs = build_event_agent_jobs(
        configs,
        {"type": "feishu_message", "project": "demo", "payload": {"text": "状态"}},
    )

    assert failed_jobs[0]["name"] == "failed_task_triage"
    assert failed_jobs[0]["prompt"] == "Task 7 failed: Repair CLI / pytest failed"
    assert failed_jobs[0]["trigger"]["type"] == "event"
    assert failed_jobs[0]["trigger"]["event_type"] == "task.failed"
    assert failed_jobs[0]["source_event"]["payload"]["task_id"] == 7
    assert completed_jobs[0]["name"] == "completed_summary"
    assert commit_jobs[0]["prompt"] == "Review commit abc123 by dev."
    assert feishu_jobs[0]["agent"] == "opencode"


def test_build_event_agent_jobs_skips_disabled_agents():
    configs = {
        "disabled_failed_task_triage": EventAgentConfig(
            enabled=False,
            trigger="task.failed",
            agent="codex",
            prompt="Should not render {{ task_id }}.",
        ),
    }

    jobs = build_event_agent_jobs(
        configs,
        {"type": "task.failed", "project": "demo", "payload": {"task_id": 7}},
    )

    assert jobs == []
