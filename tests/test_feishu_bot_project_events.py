from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from click.testing import CliRunner

from codepilot.commands import feishu as feishu_cmd
from codepilot.feishu_bot import handle_command_text, handle_event_payload
from codepilot.storage import database as db
from tests.feishu_bot_testkit import _button_commands, _setup_project


def test_feishu_project_delete_requires_confirm_token(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="删除项目测试任务",
        content="验证 project delete",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    first = handle_command_text("project delete demo", chat_id="chat-project-delete")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    pending = db.get_service_state("feishu_confirm", "chat-project-delete")
    token = pending["meta"]["token"]
    second = handle_command_text(f"confirm {token}", chat_id="chat-project-delete")
    second_payload = json.dumps(second["card"], ensure_ascii=False)

    assert "敏感操作待确认" in first_payload
    assert "工作目录不会被删除" in first_payload
    assert "项目已删除" in second_payload
    assert db.get_project("demo") is None
    assert project_path.exists()

def test_feishu_project_info_card_shows_path_stats_and_service_state(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    done = db.create_task(
        project="demo",
        title="已完成项目任务",
        content="验证项目信息卡片",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )
    db.update_task(done["id"], status="done", completed_at=datetime.now().isoformat())

    reply = handle_command_text("project info demo")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    registered_path = db.get_project("demo")["path"].replace("\\", "\\\\")

    assert reply["type"] == "interactive"
    assert "CodePilot 项目信息" in payload
    assert registered_path in payload
    assert "任务总数" in payload
    assert "完成" in payload
    assert "任务轮询" in payload
    assert "未运行" in payload
    assert "project delete demo" in payload
    assert "detail <id>" not in payload
    assert "logs <id>" not in payload

def test_feishu_project_add_registers_and_refreshes_project_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    project_path = tmp_path / "my project"
    project_path.mkdir()

    created = handle_command_text(f'project add remote "{project_path}"', chat_id="chat-project-add")
    created_payload = json.dumps(created["card"], ensure_ascii=False)
    project = db.get_project("remote")

    assert created["type"] == "interactive"
    assert "项目已注册" in created_payload
    assert project is not None
    assert project["path"] == str(project_path.resolve())
    assert Path(project["config_file"]).exists()

    config_file = project["config_file"]
    db.register_project("remote", str(project_path.resolve()), config_file=None)
    updated = handle_command_text(f'project add remote "{project_path}"', chat_id="chat-project-add")
    updated_payload = json.dumps(updated["card"], ensure_ascii=False)
    refreshed = db.get_project("remote")

    assert "项目已更新" in updated_payload
    assert refreshed["config_file"] == config_file

def test_feishu_project_commands_return_clear_error_cards(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    another_path = tmp_path / "another"
    another_path.mkdir()
    missing_path = tmp_path / "missing"

    invalid = handle_command_text(f'project add bad "{missing_path}"')
    duplicate = handle_command_text(f'project add demo "{another_path}"')
    missing = handle_command_text("project info missing-project")
    invalid_payload = json.dumps(invalid["card"], ensure_ascii=False)
    duplicate_payload = json.dumps(duplicate["card"], ensure_ascii=False)
    missing_payload = json.dumps(missing["card"], ensure_ascii=False)

    assert invalid["type"] == "interactive"
    assert "项目注册失败" in invalid_payload
    assert "不存在或不是目录" in invalid_payload

    assert duplicate["type"] == "interactive"
    assert "项目注册失败" in duplicate_payload
    assert "已注册到" in duplicate_payload

    assert missing["type"] == "interactive"
    assert "项目信息不可用" in missing_payload
    assert "未注册" in missing_payload

def test_feishu_unknown_plain_text_defaults_to_chat_goal(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda project, text, *, source, external_session_id: calls.append(
            {"project": project, "text": text, "source": source, "external_session_id": external_session_id}
        )
        or {"ok": True, "message": "这里按 OpenCode 会话处理。", "opencode_session_id": "ses-feishu"},
    )

    reply = handle_command_text("wat")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    session_id = str(db.list_sessions(project="demo")[0]["id"])

    assert reply["type"] == "interactive"
    assert calls == [{"project": "demo", "text": "wat", "source": "feishu", "external_session_id": session_id}]
    assert "按 OpenCode 会话处理" in payload

def test_feishu_plain_text_without_active_project_prompts_project_choice(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    other_path = tmp_path / "other"
    other_path.mkdir()
    db.register_project("other", str(other_path))

    reply = handle_command_text("帮我看看最近进度", chat_id="chat-multi")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "先确认你要在哪个项目里继续" in payload
    assert "点击候选按钮继续" in payload
    assert {"1", "2"}.issubset(set(_button_commands(reply["card"])))

def test_feishu_event_payload_dedupes_same_message_id(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    monkeypatch.setattr(
        "codepilot.feishu_bot.handle_command_text",
        lambda text, **kwargs: calls.append({"text": text, "kwargs": kwargs}) or {
            "type": "text",
            "text": "ok",
        },
    )

    payload = {"chat_id": "chat-dup", "message_id": "msg-1", "text": "hello"}
    first = handle_event_payload(payload)
    second = handle_event_payload(payload)

    assert first["type"] == "text"
    assert second == {"type": "ignore"}
    assert calls == [{"text": "hello", "kwargs": {"config_path": None, "chat_id": "chat-dup"}}]

def test_feishu_event_payload_prefers_event_id_for_dedup(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []

    monkeypatch.setattr(
        "codepilot.feishu_bot.handle_command_text",
        lambda text, **kwargs: calls.append({"text": text, "kwargs": kwargs}) or {
            "type": "text",
            "text": "ok",
        },
    )

    payload = {"chat_id": "chat-dup", "event_id": "evt-1", "message_id": "msg-1", "text": "hello"}
    first = handle_event_payload(payload)
    second = handle_event_payload(payload)

    assert first["type"] == "text"
    assert second == {"type": "ignore"}
    assert calls == [{"text": "hello", "kwargs": {"config_path": None, "chat_id": "chat-dup"}}]

def test_feishu_event_payload_failure_keeps_dedup_record(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "codepilot.feishu_bot.handle_command_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    payload = {"chat_id": "chat-dup", "event_id": "evt-fail-1", "message_id": "msg-fail-1", "text": "hello"}
    first = handle_event_payload(payload)
    second = handle_event_payload(payload)
    state = db.get_service_state("feishu_inbound_msg", "event:evt-fail-1")

    assert first["type"] == "interactive"
    assert second == {"type": "ignore"}
    assert state
    assert state["status"] == "failed"

def test_feishu_handle_event_cli_emits_clean_json_even_with_stdout_noise(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    def _noisy_handler(payload, **kwargs):
        print("noise on stdout")
        return {"type": "text", "text": "ok"}

    monkeypatch.setattr(feishu_cmd, "handle_event_payload", _noisy_handler)

    runner = CliRunner()
    result = runner.invoke(
        feishu_cmd.handle_event_cmd,
        input=json.dumps({"chat_id": "chat-1", "message_id": "msg-1", "text": "hello"}, ensure_ascii=False),
    )

    assert result.exit_code == 0
    last_line = [line for line in result.output.splitlines() if line.strip()][-1]
    assert json.loads(last_line) == {"type": "text", "text": "ok"}

def test_submit_goal_action_without_webui_server_uses_fallback_ui_state(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    import sys
    from codepilot.webapp import actions as web_actions

    monkeypatch.setitem(sys.modules, "codepilot.webapp.server", None)
    web_actions._UI_JOB_SEQ = 0
    web_actions._UI_JOBS.clear()
    web_actions._UI_EVENTS.clear()

    monkeypatch.setattr(
        "codepilot.webapp.actions.assess_requirement_for_planning",
        lambda title, **kwargs: {"status": "ready", "refined_title": title},
    )
    monkeypatch.setattr(
        web_actions,
        "run_requirement_workflow",
        lambda **kwargs: {"tasks": [], "run": {"done": 0, "failed": 0}, "summary": "ok"},
    )
    monkeypatch.setattr(
        "codepilot.webapp.action_requirements._start_requirement_job_process",
        lambda job_id, project_info: type("Proc", (), {"pid": 4321})(),
    )

    result = web_actions.submit_goal_action("demo", "帮我整理任务说明", category="requirement")

    assert result["ok"] is True
    assert result["job"]["id"] == 1
    assert result["job"]["status"] in {"queued", "running", "succeeded", "attention"}
