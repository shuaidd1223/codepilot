"""Matrix coverage for shared gateway config_ref propagation."""

from __future__ import annotations

from click.testing import CliRunner

from codepilot.storage import database as db
from codepilot.webapp import server as webui_mod
from codepilot.cli import main
from codepilot.commands import auto as auto_mod
from tests.ai_gateway_testkit import build_gateway_capture
from tests.chat_flow_testkit import init_test_db, register_project


def _run_webui_session_chat_question(tmp_path, monkeypatch):
    init_test_db(tmp_path, monkeypatch)
    project_path = tmp_path / "project"
    config_root = tmp_path / "config-root"
    project_path.mkdir()
    config_root.mkdir()
    config_file = config_root / "AGENTS.toml"
    config_file.write_text(
        """
[project]
name = "demo"
[classifier]
enabled = true
provider = "openai"
model = "gpt-test"
timeout = 17
""".strip(),
        encoding="utf-8",
    )
    db.register_project("demo", str(project_path), config_file=str(config_file))
    session = webui_mod.create_session_action("demo", title="chat")

    captured, fake_classify, fake_answer = build_gateway_capture(answer_text="来自会话问答路径")
    monkeypatch.setattr("codepilot.ai_support.service.classify_intent", fake_classify)
    monkeypatch.setattr("codepilot.ai_support.service.answer_question_via_api", fake_answer)

    out = webui_mod.send_session_message_action(session["session"]["id"], "随便说点什么", category="auto")

    assert out["ok"] is True
    assert out["intent"] == "question"
    assert out["message"] == "来自会话问答路径"
    return captured, str(config_file)


def _run_go_question(tmp_path, monkeypatch):
    project_path = register_project(tmp_path, monkeypatch)
    captured, _fake_classify, fake_answer = build_gateway_capture(answer_text="这是 go 的问答回复")
    ran: list[dict] = []

    monkeypatch.setattr(auto_mod, "classify_entry_intent", lambda text, **kw: "question")
    monkeypatch.setattr(auto_mod, "answer_question_via_api", fake_answer)
    monkeypatch.setattr(auto_mod, "run_requirement_workflow", lambda **kw: ran.append(kw))

    result = CliRunner().invoke(main, ["go", "这个工具怎么用"])

    assert result.exit_code == 0
    assert "这是 go 的问答回复" in result.output
    assert not ran
    return captured, str(project_path)


def test_go_question_answer_uses_project_config_ref(tmp_path, monkeypatch):
    captured, expected_config_ref = _run_go_question(tmp_path, monkeypatch)

    answer_opts = captured["answer"]["gateway_options"]
    assert answer_opts.config_ref == expected_config_ref

