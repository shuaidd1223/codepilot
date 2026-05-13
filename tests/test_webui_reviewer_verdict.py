"""Structured reviewer verdict exposure in task_detail_payload.

The dashboard renders a dedicated verdict panel (ac_checks table, blockers,
advisory) instead of dumping the raw transcript. These tests lock in the
payload shape so the Vue component stays in sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.storage import database as db
from codepilot.webapp import payloads as webui_payloads


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    db.register_project("demo", str(tmp_path))
    task = db.create_task(
        "demo",
        "Reviewer payload smoke",
        content="body",
        agent="dual",
        priority="P2",
    )
    return {"project": "demo", "task_id": task["id"]}


def _builder_log(task_id: int) -> None:
    db.create_task_log(
        task_id=task_id,
        agent="codex",
        phase="builder",
        output="builder output text\nnothing to parse here",
        exit_code=0,
    )


def _reviewer_log_with_json(task_id: int, verdict: str, blockers=None) -> None:
    body = {
        "verdict": verdict,
        "ac_checks": [{"id": "AC-1", "status": "PASS", "reason": "ok"}],
        "blockers": blockers or [],
        "advisory": [],
    }
    import json as _json

    output = (
        "AC #1: PASS (自测通过)\n"
        f"VERDICT: {verdict.upper()}\n"
        "```json\n"
        f"{_json.dumps(body, ensure_ascii=False)}\n"
        "```\n"
    )
    db.create_task_log(
        task_id=task_id,
        agent="codex-review",
        phase="reviewer",
        output=output,
        exit_code=0,
    )


def test_task_detail_attaches_review_to_reviewer_log_only(fresh_db):
    tid = fresh_db["task_id"]
    _builder_log(tid)
    _reviewer_log_with_json(tid, "pass")

    payload = webui_payloads.task_detail_payload(tid)

    assert len(payload["logs"]) == 2
    builder, reviewer = payload["logs"]

    # Builder entries have no parsed review — frontend treats as null.
    assert builder["phase"] == "builder"
    assert builder["review"] is None

    # Reviewer entries carry the parsed verdict block.
    assert reviewer["phase"] == "reviewer"
    assert reviewer["review"] is not None
    assert reviewer["review"]["verdict"] == "pass"
    assert reviewer["review"]["source"] == "json"
    assert reviewer["review"]["ac_checks"] == [
        {"id": "AC-1", "status": "PASS", "reason": "ok"}
    ]


def test_task_detail_latest_review_reflects_newest_reviewer_entry(fresh_db):
    tid = fresh_db["task_id"]
    _reviewer_log_with_json(tid, "fail", blockers=["修复超时处理"])
    _builder_log(tid)
    _reviewer_log_with_json(tid, "pass")

    payload = webui_payloads.task_detail_payload(tid)

    assert payload["latest_review"] is not None
    assert payload["latest_review"]["verdict"] == "pass"
    assert payload["latest_review"]["agent"] == "codex-review"
    assert payload["latest_review"]["phase"] == "reviewer"


def test_task_detail_latest_review_is_none_when_no_reviewer_entry(fresh_db):
    tid = fresh_db["task_id"]
    _builder_log(tid)  # no reviewer phase at all
    payload = webui_payloads.task_detail_payload(tid)

    assert payload["latest_review"] is None
    assert all(entry["review"] is None for entry in payload["logs"])


def test_task_detail_falls_back_to_legacy_parse_when_no_json_fence(fresh_db):
    """Old reviewers that only emit VERDICT + 需要修复的点 still get a structured
    block (source=legacy)."""
    tid = fresh_db["task_id"]
    db.create_task_log(
        task_id=tid,
        agent="claude-review",
        phase="reviewer",
        output=(
            "AC #1: FAIL (没看到实现)\n"
            "需要修复的点:\n"
            "- 在 status.py 加 --json 分支\n"
            "\n"
            "VERDICT: FAIL"
        ),
        exit_code=0,
    )

    payload = webui_payloads.task_detail_payload(tid)
    review = payload["latest_review"]
    assert review is not None
    assert review["verdict"] == "fail"
    assert review["source"] == "legacy"
    assert review["blockers"] == ["- 在 status.py 加 --json 分支"]


def test_task_detail_review_none_for_empty_reviewer_output(fresh_db):
    """An empty reviewer transcript must not synthesize a phantom verdict."""
    tid = fresh_db["task_id"]
    db.create_task_log(
        task_id=tid,
        agent="codex-review",
        phase="reviewer",
        output="",
        exit_code=1,
    )
    payload = webui_payloads.task_detail_payload(tid)
    assert payload["logs"][0]["review"] is None
    assert payload["latest_review"] is None


def test_task_detail_review_is_attached_when_agent_name_contains_review(fresh_db):
    """Phase is sometimes recorded as empty; agent name is the secondary signal."""
    tid = fresh_db["task_id"]
    db.create_task_log(
        task_id=tid,
        agent="codex-review",
        phase="",  # missing phase, but agent implies reviewer
        output=(
            "AC #1: PASS\n"
            "VERDICT: PASS\n"
            "```json\n"
            '{"verdict": "pass", "ac_checks": [], "blockers": [], "advisory": []}\n'
            "```\n"
        ),
        exit_code=0,
    )
    payload = webui_payloads.task_detail_payload(tid)
    assert payload["latest_review"] is not None
    assert payload["latest_review"]["verdict"] == "pass"

