from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codepilot.core.config import load_project_config
from codepilot.scheduled.daemon import run_project_agent_jobs


def test_default_scheduled_agents_dry_run_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_GLOBAL_CONFIG_PATH", str(tmp_path / "missing-global-AGENTS.toml"))
    repo_root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.toml").write_text(
        (repo_root / "AGENTS.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    config = load_project_config(project)
    assert config is not None

    scheduled = config.automation.scheduled_agents
    assert set(scheduled) >= {"task_health", "daily_summary", "auto_inspect"}
    assert scheduled["task_health"].enabled is True
    assert scheduled["task_health"].interval == "10m"
    assert scheduled["daily_summary"].enabled is True
    assert scheduled["daily_summary"].interval == "1d"
    assert "Feishu" in scheduled["daily_summary"].prompt
    assert scheduled["auto_inspect"].enabled is True
    assert scheduled["auto_inspect"].interval == "30m"

    subprocess_calls: list[Any] = []

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        subprocess_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("dry-run must not invoke an external CLI")

    summary = run_project_agent_jobs(
        {"name": "demo", "path": str(project)},
        config=config,
        dry_run=True,
        subprocess_run=fail_if_called,
        now=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
    )

    assert subprocess_calls == []
    scheduled_summary = summary["scheduled"]
    jobs = scheduled_summary["jobs"]
    results = scheduled_summary["results"]
    assert {job["name"] for job in jobs} >= {"task_health", "daily_summary", "auto_inspect"}
    for name in ("task_health", "daily_summary", "auto_inspect"):
        index = [job["name"] for job in jobs].index(name)
        assert jobs[index]["type"] == "agent_job"
        assert results[index].dry_run is True
        assert results[index].exit_code is None
        assert results[index].audit_log_path

    audit_path = project / ".codepilot" / "scheduled" / "audit.jsonl"
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    audited = {record["job_name"]: record for record in records}
    for name in ("task_health", "daily_summary", "auto_inspect"):
        assert audited[name]["dry_run"] is True
        assert audited[name]["exit_code"] is None
