"""Matrix coverage for shared gateway config_ref propagation."""

from __future__ import annotations

from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from tests.chat_flow_testkit import register_project


def test_go_entrypoint_no_longer_classifies_or_answers_questions(tmp_path, monkeypatch):
    register_project(tmp_path, monkeypatch)
    ran: list[dict] = []

    assert not hasattr(auto_mod, "classify_entry_intent")
    assert not hasattr(auto_mod, "answer_question_via_api")
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw))

    from click.testing import CliRunner

    result = CliRunner().invoke(main, ["go", "--no-execute", "这个工具怎么用"])

    assert result.exit_code == 0, result.output
    assert ran
    assert ran[0]["title"] == "这个工具怎么用"
    assert ran[0]["execute"] is False
