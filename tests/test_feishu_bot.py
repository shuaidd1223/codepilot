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
    """验证 feishu_worker.mjs 在处理消息前发送处理中即时反馈卡片。"""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.mjs"
    content = worker_path.read_text(encoding="utf-8")

    # 必须有 buildProcessingCard 函数
    assert "function buildProcessingCard" in content, "worker 必须包含 buildProcessingCard 函数"
    assert "已收到您的消息" in content, "处理中卡片应包含中文提示"
    # 验证卡片现在显示用户原文
    assert "「" in content, "处理中卡片应以「」展示用户原文"

    # processIncomingMessage 必须在 invokePython 之前调用 buildProcessingCard
    msg_start = content.index("async function processIncomingMessage")
    msg_body = content[msg_start:content.index("\nfunction cardActionEvent", msg_start)]
    processing_call = msg_body.index("buildProcessingCard(")
    python_call = msg_body.index("invokePython")
    assert processing_call < python_call, (
        "processIncomingMessage 必须在 invokePython 前发送处理中卡片"
    )

    # processCardAction 同样在 invokePython 前调用 buildProcessingCard
    card_start = content.index("async function processCardAction")
    card_body = content[card_start:content.index("\nfunction buildPostContent", card_start)]
    card_processing_call = card_body.index("buildProcessingCard(")
    card_python_call = card_body.index("invokePython")
    assert card_processing_call < card_python_call, (
        "processCardAction 必须在 invokePython 前发送处理中卡片"
    )

    # sendReply 现在应返回 message_id
    assert "return res?.data?.message_id || null" in content, "sendReply 应返回 message_id"
    # updateReply 使用 client.im.message.patch 更新交互卡片；update 只支持文本/富文本。
    assert "async function updateReply" in content, "worker 必须包含 updateReply 函数"
    assert "client.im.message.patch" in content, "updateReply 应调用 im.message.patch API"


def test_feishu_worker_patches_interactive_cards_instead_of_text_update():
    """处理中卡片是 interactive，不能用只支持文本/富文本的 update 接口。"""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.mjs"
    content = worker_path.read_text(encoding="utf-8")

    update_start = content.index("async function updateReply")
    update_body = content[update_start:content.index("\nconst dispatcher", update_start)]

    assert "client.im.message.patch" in update_body
    assert "data: {" in update_body
    assert "content: JSON.stringify(card)" in update_body
    assert "msg_type: 'interactive'" not in update_body
    assert "client.im.message.update" not in update_body


def test_feishu_worker_sends_processing_feedback_before_python():
    """验证 feishu_worker.mjs 在处理消息前会先发送「处理中」反馈卡片。"""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.mjs"
    content = worker_path.read_text(encoding="utf-8")

    # 必须包含 buildProcessingCard 函数定义
    assert "function buildProcessingCard" in content, "worker 必须定义 buildProcessingCard 函数"

    # processIncomingMessage: 先 buildProcessingCard 再 invokePython
    incoming_lines = []
    in_incoming = False
    brace_depth = 0
    for line in content.splitlines():
        stripped = line.strip()
        if "async function processIncomingMessage" in stripped:
            in_incoming = True
            brace_depth = 0
        if in_incoming:
            incoming_lines.append(stripped)
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0 and stripped == "}" and len(incoming_lines) > 3:
                break

    incoming_body = "\n".join(incoming_lines)
    processing_call_index = incoming_body.find("buildProcessingCard")
    invoke_index = incoming_body.find("invokePython")
    assert processing_call_index >= 0, "processIncomingMessage 必须调用 buildProcessingCard"
    assert invoke_index >= 0, "processIncomingMessage 必须调用 invokePython"
    assert processing_call_index < invoke_index, (
        f"buildProcessingCard（位置 {processing_call_index}）必须在 "
        f"invokePython（位置 {invoke_index}）之前调用"
    )
    # 验证处理完成后会 updateReply 更新卡片（而不是发新消息）
    assert "updateReply" in incoming_body, "processIncomingMessage 应使用 updateReply 更新处理中卡片"

    # 同样检查 processCardAction
    card_lines = []
    in_card = False
    brace_depth = 0
    for line in content.splitlines():
        stripped = line.strip()
        if "async function processCardAction" in stripped:
            in_card = True
            brace_depth = 0
        if in_card:
            card_lines.append(stripped)
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0 and stripped == "}" and len(card_lines) > 3:
                break

    card_body = "\n".join(card_lines)
    card_processing_idx = card_body.find("buildProcessingCard")
    card_invoke_idx = card_body.find("invokePython")
    assert card_processing_idx >= 0, "processCardAction 必须调用 buildProcessingCard"
    assert card_invoke_idx >= 0, "processCardAction 必须调用 invokePython"
    assert card_processing_idx < card_invoke_idx, (
        f"processCardAction 中 buildProcessingCard（位置 {card_processing_idx}）必须在 "
        f"invokePython（位置 {card_invoke_idx}）之前调用"
    )
    # processCardAction 也使用 updateReply
    assert "updateReply" in card_body, "processCardAction 应使用 updateReply 更新处理中卡片"


def test_feishu_worker_switches_python_args_for_frozen_binary():
    """冻结二进制运行时不能继续向 codepilot.exe 传入 python -m 参数。"""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.mjs"
    content = worker_path.read_text(encoding="utf-8")

    assert "CODEPILOT_FEISHU_PYTHON_MODE" in content
    assert "pythonMode === 'binary'" in content
    assert "['feishu', 'handle-event']" in content
    assert "['-m', 'codepilot', 'feishu', 'handle-event']" in content


def test_feishu_worker_processing_card_shows_user_text():
    """验证 buildProcessingCard 在传入用户原文时，卡片中会回显原文。"""
    worker_path = Path(__file__).resolve().parents[1] / "codepilot" / "feishu_worker.mjs"
    content = worker_path.read_text(encoding="utf-8")

    # 卡片 body 会形如「用户原文」，回显用户输入
    assert "「" in content and "」" in content, "处理中卡片应以「」回显用户原文"
    assert "raw.slice" in content or "正在处理中" in content, "处理中卡片应包含处理中提示"

    # 处理中卡片更新脚注提示卡片会被自动更新
    assert "会自动更新此卡片" in content, "处理中卡片应提示会自动更新"

    # processIncomingMessage 在卡片回复时使用 updateReply（而非发送新消息）
    msg_start = content.index("async function processIncomingMessage")
    msg_end = content.index("async function processCardAction")
    msg_body = content[msg_start:msg_end]
    # 核心路径：卡片回复时走 updateReply
    assert "updateReply(chatId, processingMsgId, reply.card)" in msg_body, (
        "processIncomingMessage 在卡片回复时应直接更新处理中卡片为最终结果"
    )
    # 错误时也走 updateReply
    assert "updateReply(chatId, processingMsgId, buildErrorCard" in content, (
        "处理失败时应使用 updateReply 更新处理中卡片为错误状态"
    )

    # processCardAction 也使用 updateReply
    card_start = content.index("async function processCardAction")
    card_body = content[card_start:content.index("\nfunction buildPostContent", card_start)]
    assert "updateReply" in card_body, "processCardAction 应使用 updateReply 更新处理中卡片"
