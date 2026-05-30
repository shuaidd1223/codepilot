from __future__ import annotations

import importlib
from pathlib import Path


def test_feishu_bot_refactor_modules_keep_legacy_entrypoints():
    config_module = importlib.import_module("codepilot.feishu_config")
    cards_module = importlib.import_module("codepilot.feishu_cards")
    commands_module = importlib.import_module("codepilot.feishu_commands")
    interactions_module = importlib.import_module("codepilot.feishu_interactions")
    bot_module = importlib.import_module("codepilot.feishu_bot")

    assert bot_module.FeishuBotConfig is config_module.FeishuBotConfig
    assert bot_module._card is cards_module.card
    assert bot_module._normalize_command_text is commands_module.normalize_command_text
    assert bot_module._normalize_command_text("任务日志 #12", "") == "logs 12"
    assert interactions_module.card_action_command({"action": {"value": {"cmd": "tasks demo"}}}) == "tasks demo"
    assert interactions_module.card_action_chat_id({"context": {"open_chat_id": "chat-1"}}) == "chat-1"
    assert len(Path(bot_module.__file__).read_text(encoding="utf-8").splitlines()) < 2860


def test_feishu_worker_sends_processing_feedback():
    """Verify the Python worker sends a processing card before handling a message."""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.py"
    content = worker_path.read_text(encoding="utf-8")

    assert "_build_processing_card" in content, "worker must define _build_processing_card"
    assert "已收到您的消息" in content, "processing card must contain Chinese hint"
    assert "「" in content, "processing card must use 「」 for user text display"

    # send_reply must return message_id (from create_message response)
    assert "resp.data.message_id" in content, "send_reply must return message_id"

    # update_reply must use im.message.patch for interactive cards
    assert "update_reply" in content, "worker must have update_reply method"
    assert "message.patch" in content, "update_reply must call im.message.patch API"


def test_feishu_worker_patches_interactive_cards_instead_of_text_update():
    """Processing cards are interactive; must use patch, not update API."""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.py"
    content = worker_path.read_text(encoding="utf-8")

    assert "PatchMessageRequest" in content
    assert "PatchMessageRequestBody" in content
    assert "message.patch" in content
    # Must NOT use the update (text-only) API
    assert "message.update" not in content


def test_feishu_worker_sends_processing_feedback_before_handling():
    """Verify _process_incoming_message sends processing card before calling handler."""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.py"
    content = worker_path.read_text(encoding="utf-8")

    assert "_build_processing_card" in content

    # _process_incoming_message: send_reply (processing card) before handle_event_payload
    msg_start = content.index("def _process_incoming_message")
    msg_body = content[msg_start:content.index("\n    # ── Main entry", msg_start)]
    processing_idx = msg_body.index("_build_processing_card")
    handle_idx = msg_body.index("handle_event_payload")
    assert processing_idx < handle_idx, (
        "must send processing card before calling handle_event_payload"
    )
    assert "update_reply" in msg_body, "must use update_reply for card updates"


def test_feishu_worker_direct_python_call_no_subprocess():
    """The Python worker calls handle_event_payload directly, no subprocess per message."""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.py"
    content = worker_path.read_text(encoding="utf-8")

    # No Node.js subprocess spawn calls
    assert "spawnSync" not in content
    # Import from feishu_bot directly
    assert "from codepilot.feishu_bot import" in content


def test_feishu_worker_processing_card_shows_user_text():
    """Verify _build_processing_card echoes user's original text."""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.py"
    content = worker_path.read_text(encoding="utf-8")

    assert "「" in content and "」" in content, "processing card should use 「」to echo user text"
    assert "正在处理中" in content, "processing card must show processing hint"
    assert "会自动更新此卡片" in content, "processing card must hint it will auto-update"

    # Core path: card reply -> update_reply instead of new message
    msg_start = content.index("def _process_incoming_message")
    run_start = content.index("def run", msg_start)
    msg_body = content[msg_start:run_start]
    assert "update_reply" in msg_body, "must use update_reply for card results"
    assert "_build_error_card" in content, "must build error cards on failure"
