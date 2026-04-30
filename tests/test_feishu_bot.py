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
