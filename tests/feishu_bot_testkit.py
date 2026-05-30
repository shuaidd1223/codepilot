from __future__ import annotations

import json

from codepilot.storage import database as db


def _setup_project(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    db.init_db()
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def _card_buttons(card):
    buttons = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("tag") == "button":
                buttons.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(card)
    return buttons


def _button_commands(card):
    return [button.get("value", {}).get("command") for button in _card_buttons(card)]


def _button_types_by_command(card):
    return {button.get("value", {}).get("command"): button.get("type") for button in _card_buttons(card)}


def _action_blocks(card):
    return [elem for elem in card["elements"] if isinstance(elem, dict) and elem.get("tag") == "action"]


def _assert_multi_button_actions_use_flow_layout(card):
    for block in _action_blocks(card):
        if len(block.get("actions", [])) > 1:
            assert block.get("layout") == "flow"


def _assert_card_uses_markdown(card):
    payload = json.dumps(card, ensure_ascii=False)
    assert "lark_md" in payload


def _first_interactive_card(reply):
    if reply["type"] == "interactive":
        return reply["card"]
    if reply["type"] == "multi":
        for message in reply["messages"]:
            if message.get("type") == "interactive":
                return message["card"]
    raise AssertionError(f"reply does not contain an interactive card: {reply!r}")


def _assert_no_copy_command_panel(card):
    payload = json.dumps(card, ensure_ascii=False)
    assert "复制命令发送即可执行" not in payload
    assert "下一步命令" not in payload
