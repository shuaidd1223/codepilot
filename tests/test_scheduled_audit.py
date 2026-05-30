from __future__ import annotations

import json
from pathlib import Path

from codepilot.scheduled.audit import append_agent_job_audit, prompt_hash


def test_append_agent_job_audit_creates_directory_and_records_prompt_hash_only(tmp_path: Path):
    log_path = append_agent_job_audit(
        tmp_path,
        {
            "trigger": {"type": "event", "event_type": "task.failed"},
            "agent": "codex",
            "prompt": "Sensitive full prompt with project context.",
            "tools": [{"name": "Read"}, {"name": "Bash"}],
            "cost": 0.04,
            "exit_code": 0,
        },
        now="2026-05-09T02:40:00+08:00",
    )

    assert log_path == tmp_path / ".codepilot" / "scheduled" / "audit.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["timestamp"] == "2026-05-09T02:40:00+08:00"
    assert record["trigger"] == {"type": "event", "event_type": "task.failed"}
    assert record["agent"] == "codex"
    assert record["prompt_hash"] == prompt_hash("Sensitive full prompt with project context.")
    assert "prompt" not in record
    assert record["tools"] == [{"name": "Read"}, {"name": "Bash"}]
    assert record["cost"] == 0.04
    assert record["exit_code"] == 0
    assert "Sensitive full prompt" not in lines[0]


def test_append_agent_job_audit_appends_json_lines(tmp_path: Path):
    append_agent_job_audit(
        tmp_path,
        {"trigger": {"type": "interval"}, "agent": "claude", "prompt": "first"},
        now="2026-05-09T02:40:00+08:00",
    )
    append_agent_job_audit(
        tmp_path,
        {"trigger": {"type": "schedule"}, "agent": "opencode", "prompt": "second"},
        now="2026-05-09T02:41:00+08:00",
    )

    lines = (tmp_path / ".codepilot" / "scheduled" / "audit.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    records = [json.loads(line) for line in lines]

    assert [record["agent"] for record in records] == ["claude", "opencode"]
    assert records[0]["prompt_hash"] != records[1]["prompt_hash"]
