from __future__ import annotations

import json
from datetime import datetime, timedelta

from codepilot.feishu_bot import (
    build_batch_task_action_card,
    handle_card_action_payload,
    handle_command_text,
)
from codepilot.storage import database as db
from tests.feishu_bot_testkit import (
    _assert_card_uses_markdown,
    _button_commands,
    _first_interactive_card,
    _setup_project,
)


def test_feishu_delete_task_command_requires_confirm_token(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="删除飞书测试任务",
        content="验证删除命令",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    first = handle_command_text(f"delete {task['id']}", chat_id="chat-delete")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    pending = db.get_service_state("feishu_confirm", "chat-delete")
    token = pending["meta"]["token"]

    assert first["type"] == "interactive"
    assert "敏感操作待确认" in first_payload
    assert f"confirm {token}" in _button_commands(first["card"])
    assert f"`confirm {token}`" not in first_payload
    assert db.get_task(task["id"]) is not None
    second = handle_command_text(f"confirm {token}", chat_id="chat-delete")
    second_payload = json.dumps(second["card"], ensure_ascii=False)
    assert second["type"] == "interactive"
    assert "任务已删除" in second_payload
    assert f"#{task['id']}" in second_payload
    assert db.get_task(task["id"]) is None

def test_feishu_batch_cancel_task_command_returns_summary_card(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    first = db.create_task(
        project="demo",
        title="批量取消任务 1",
        content="验证飞书批量取消",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    second = db.create_task(
        project="demo",
        title="批量取消任务 2",
        content="验证飞书批量取消",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    reply = handle_command_text(f"cancel {first['id']} {second['id']}")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "批量取消完成" in payload
    assert "成功" in payload
    assert db.get_task(first["id"])["status"] == "cancelled"
    assert db.get_task(second["id"])["status"] == "cancelled"

def test_feishu_batch_archive_task_command_accepts_comma_separated_ids(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    first = db.create_task(
        project="demo",
        title="批量归档任务 1",
        content="验证飞书批量归档",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    second = db.create_task(
        project="demo",
        title="批量归档任务 2",
        content="验证飞书批量归档",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.update_task(first["id"], status="done")
    db.update_task(second["id"], status="done")

    reply = handle_command_text(f"archive {first['id']},{second['id']}")
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "批量归档完成" in payload
    assert db.get_task(first["id"])["status"] == "archived"
    assert db.get_task(second["id"])["status"] == "archived"

def test_feishu_batch_delete_task_command_reports_partial_failures(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    deletable = db.create_task(
        project="demo",
        title="批量删除任务 1",
        content="验证飞书批量删除",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    blocked = db.create_task(
        project="demo",
        title="批量删除任务 2",
        content="验证飞书批量删除",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.update_task(deletable["id"], status="done")
    db.update_task(blocked["id"], status="in_progress")

    first = handle_command_text(f"delete {deletable['id']} {blocked['id']}", chat_id="chat-batch-delete")
    first_payload = json.dumps(first["card"], ensure_ascii=False)
    pending = db.get_service_state("feishu_confirm", "chat-batch-delete")
    token = pending["meta"]["token"]

    assert first["type"] == "interactive"
    assert "敏感操作待确认" in first_payload
    assert "批量删除 2 个任务" in first_payload
    assert db.get_task(deletable["id"]) is not None
    second = handle_command_text(f"confirm {token}", chat_id="chat-batch-delete")
    second_payload = json.dumps(second["card"], ensure_ascii=False)
    assert second["type"] == "interactive"
    assert "批量删除完成" in second_payload
    assert "失败 1" in second_payload
    assert db.get_task(deletable["id"]) is None
    assert db.get_task(blocked["id"])["status"] == "in_progress"

def test_build_batch_task_action_card_contract_for_partial_failure():
    card = build_batch_task_action_card(
        "archive",
        {
            "total": 3,
            "success_count": 2,
            "failed_count": 1,
            "message": "批量archive完成：成功 2，失败 1。",
            "succeeded": [
                {"task_id": 11, "message": "任务 #11 已归档。", "task": {"id": 11}},
                {"task_id": 12, "message": "任务 #12 已归档。", "task": {"id": 12}},
            ],
            "failed": [{"task_id": 13, "error": "任务 #13 正在执行中，不能归档。"}],
        },
        prefix="/cp",
    )
    payload = json.dumps(card, ensure_ascii=False)

    assert card["header"]["template"] == "orange"
    assert card["header"]["title"]["content"] == "批量归档完成"
    assert "飞书已执行批量任务操作，并返回逐项结果。" in payload
    assert "**操作**" in payload
    assert "`批量归档`" in payload
    assert "**总计**" in payload
    assert "`3`" in payload
    assert "成功任务" in payload
    assert "- `#11` 任务 #11 已归档。" in payload
    assert "失败任务" in payload
    assert "- `#13` 任务 #13 正在执行中，不能归档。" in payload
    assert {"/cp detail 11", "/cp logs 11", "/cp tasks"}.issubset(set(_button_commands(card)))
    assert "`/cp detail 11`" not in payload
    assert "`/cp logs 11`" not in payload
    assert "`/cp tasks`" not in payload

def test_build_batch_task_action_card_delete_omits_detail_commands_and_limits_rows():
    succeeded = [
        {"task_id": task_id, "message": f"任务 #{task_id} 已删除。", "deleted_task_id": task_id}
        for task_id in range(1, 11)
    ]
    card = build_batch_task_action_card(
        "delete",
        {
            "total": 10,
            "success_count": 10,
            "failed_count": 0,
            "message": "批量delete完成：共 10 个任务。",
            "succeeded": succeeded,
            "failed": [],
        },
    )
    payload = json.dumps(card, ensure_ascii=False)

    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "批量删除完成"
    assert "tasks" in _button_commands(card)
    assert "detail 1" not in _button_commands(card)
    assert "logs 1" not in _button_commands(card)
    assert "`tasks`" not in payload
    assert "- `#8` 任务 #8 已删除。" in payload
    assert "- `#9` 任务 #9 已删除。" not in payload

def test_feishu_natural_language_status_uses_opencode(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda project, text, *, source, external_session_id: calls.append(
            {"project": project, "text": text, "source": source, "external_session_id": external_session_id}
        )
        or {"ok": True, "message": "OpenCode 已查看 demo 项目的运行状态。", "opencode_session_id": "ses-status"},
    )

    reply = handle_command_text("看看 demo 项目的运行状态")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    session_id = str(db.list_sessions(project="demo")[0]["id"])

    assert reply["type"] == "interactive"
    assert calls == [
        {"project": "demo", "text": "看看 demo 项目的运行状态", "source": "feishu", "external_session_id": session_id}
    ]
    assert "OpenCode 已查看 demo 项目的运行状态" in payload
    assert "demo" in payload

def test_feishu_short_chinese_command_aliases_do_not_fall_back_to_help(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("快捷命令不应进入 OpenCode")),
    )
    db.create_task(
        project="demo",
        title="短中文命令识别",
        content="验证飞书入口不会误判为未知命令",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )

    cases = {
        "项目": "CodePilot 项目",
        "看项目": "CodePilot 项目",
        "任务": "CodePilot 任务面板",
        "看任务": "CodePilot 任务面板",
        "服务": "项目服务状态",
        "全局": "CodePilot 全局状态",
        "当前项目": "CodePilot 项目总览",
    }

    for text, expected in cases.items():
        reply = handle_command_text(text, chat_id="chat-alias")
        payload = json.dumps(reply["card"], ensure_ascii=False)
        assert expected in payload
        assert "未识别命令" not in payload

def test_feishu_chinese_task_action_aliases_extract_task_id(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="中文任务操作识别",
        content="验证任务详情和日志中文命令",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )

    detail = handle_command_text(f"任务详情 {task['id']}")
    logs = handle_command_text(f"任务日志 {task['id']}")
    detail_payload = json.dumps(detail["card"], ensure_ascii=False)
    logs_card = _first_interactive_card(logs)
    logs_payload = json.dumps(logs_card, ensure_ascii=False)

    assert "任务详情" in detail_payload
    assert "中文任务操作识别" in detail_payload
    assert "任务日志" in logs_payload
    assert "中文任务操作识别" in logs_payload

def test_feishu_task_log_command_sends_card_and_post_rich_text(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="富文本日志任务",
        content="验证日志拆成富文本",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )
    db.update_task(task["id"], last_output="\n".join(f"日志行 {index}" for index in range(1, 12)))

    reply = handle_command_text(f"logs {task['id']}")

    assert reply["type"] == "multi"
    assert reply["messages"][0]["type"] == "interactive"
    assert reply["messages"][1]["type"] == "post"
    card = reply["messages"][0]["card"]
    post = reply["messages"][1]
    _assert_card_uses_markdown(card)
    assert "任务日志" in card["header"]["title"]["content"]
    assert post["title"] == f"任务日志 · #{task['id']}"
    assert post["content"][0][0] == {"tag": "text", "text": "日志行 1"}
    assert "日志行 11" in json.dumps(post["content"], ensure_ascii=False)
    assert {"detail 1", "logs 1"}.issubset(set(_button_commands(card)))

def test_feishu_card_action_callback_runs_button_command(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="按钮回调任务列表",
        content="验证卡片按钮点击能执行 value.command",
        agent="dual",
        priority="P1",
        project_path=str(project_path),
    )

    reply = handle_card_action_payload(
        {
            "context": {"open_chat_id": "chat-card", "open_message_id": "om_1"},
            "operator": {"open_id": "ou_1"},
            "action": {"tag": "button", "value": {"command": "tasks demo"}},
        }
    )
    payload = json.dumps(reply["card"], ensure_ascii=False)

    assert reply["type"] == "interactive"
    assert "CodePilot 任务面板" in payload
    assert "按钮回调任务列表" in payload

def test_feishu_natural_language_delete_uses_opencode(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="自然语言删除任务 1",
        content="验证飞书候选选择",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.create_task(
        project="demo",
        title="自然语言删除任务 2",
        content="验证飞书候选选择",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    handle_command_text("use demo", chat_id="chat-nl-delete")
    calls = []
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda project, text, *, source, external_session_id: calls.append(
            {"project": project, "text": text, "source": source, "external_session_id": external_session_id}
        )
        or {"ok": True, "message": "OpenCode 已接管删除请求。", "opencode_session_id": "ses-delete"},
    )
    first_reply = handle_command_text("删除任务", chat_id="chat-nl-delete")
    first_payload = json.dumps(first_reply["card"], ensure_ascii=False)
    session_id = str(db.list_sessions(project="demo")[0]["id"])

    assert calls == [
        {"project": "demo", "text": "删除任务", "source": "feishu", "external_session_id": session_id}
    ]
    assert "OpenCode 已接管删除请求" in first_payload
    assert "请确认操作" not in first_payload

def test_feishu_plain_delete_text_does_not_create_pending_choice(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    db.create_task(
        project="demo",
        title="待选择删除任务",
        content="验证数字越界不会清空待选项",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )
    db.create_task(
        project="demo",
        title="另一个待选择任务",
        content="验证数字越界不会清空待选项",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    handle_command_text("use demo", chat_id="chat-nl-out-of-range")
    calls = []
    monkeypatch.setattr(
        "codepilot.opencode.session.run_opencode_message",
        lambda project, text, *, source, external_session_id: calls.append(
            {"project": project, "text": text, "source": source, "external_session_id": external_session_id}
        )
        or {"ok": True, "message": "OpenCode 已接收删除任务请求。", "opencode_session_id": "ses-delete-choice"},
    )
    first_reply = handle_command_text("删除任务", chat_id="chat-nl-out-of-range")
    first_payload = json.dumps(first_reply["card"], ensure_ascii=False)
    session_id = str(db.list_sessions(project="demo")[0]["id"])

    assert calls == [
        {"project": "demo", "text": "删除任务", "source": "feishu", "external_session_id": session_id}
    ]
    assert db.get_service_state("feishu_confirm", "chat-nl-out-of-range") is None
    assert "OpenCode 已接收删除任务请求" in first_payload
    assert "请确认操作" not in first_payload

def test_feishu_cancel_confirm_token_keeps_task_unchanged(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="取消确认测试任务",
        content="验证 cancel confirm",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    handle_command_text(f"delete {task['id']}", chat_id="chat-cancel-confirm")
    pending = db.get_service_state("feishu_confirm", "chat-cancel-confirm")
    token = pending["meta"]["token"]
    reply = handle_command_text(f"cancel confirm {token}", chat_id="chat-cancel-confirm")
    payload = json.dumps(reply["card"], ensure_ascii=False)
    confirm_reply = handle_command_text(f"confirm {token}", chat_id="chat-cancel-confirm")
    confirm_payload = json.dumps(confirm_reply["card"], ensure_ascii=False)

    assert "确认已取消" in payload
    assert db.get_task(task["id"]) is not None
    assert "确认已失效" in confirm_payload
    assert db.get_task(task["id"]) is not None

def test_feishu_confirm_rejects_chat_mismatch_and_expired_token(tmp_path, monkeypatch):
    project_path = _setup_project(tmp_path, monkeypatch)
    task = db.create_task(
        project="demo",
        title="确认上下文测试任务",
        content="验证 chat mismatch / expired",
        agent="dual",
        priority="P2",
        project_path=str(project_path),
    )

    handle_command_text(f"delete {task['id']}", chat_id="chat-a")
    pending = db.get_service_state("feishu_confirm", "chat-a")
    token = pending["meta"]["token"]

    mismatch = handle_command_text(f"confirm {token}", chat_id="chat-b")
    mismatch_payload = json.dumps(mismatch["card"], ensure_ascii=False)
    assert "确认已失效" in mismatch_payload
    assert db.get_task(task["id"]) is not None

    pending["meta"]["expires_at"] = (datetime.now() - timedelta(seconds=5)).isoformat(timespec="seconds")
    db.upsert_service_state("feishu_confirm", "chat-a", pid=0, status="pending", log_path="", meta=pending["meta"])
    expired = handle_command_text(f"confirm {token}", chat_id="chat-a")
    expired_payload = json.dumps(expired["card"], ensure_ascii=False)

    assert "确认已失效" in expired_payload
    assert db.get_task(task["id"]) is not None
