"""Shared launcher prompt rules for user-visible language."""

from __future__ import annotations


CHINESE_INTERACTION_INSTRUCTIONS = """\
请遵守以下交互规则：
- 所有面向用户的回答、执行过程说明、澄清问题、权限申请理由、错误解释和思考/推理摘要都使用简体中文。
- 如果界面展示 thinking / reasoning / analysis 内容，只输出中文摘要；不要使用 `Thinking`、`Reasoning`、`Analysis`、`I need to...` 这类英文标题或句式。
- 代码标识符、命令、文件路径、API 名称、MCP tool 名称和原始错误码可以保留英文；解释这些内容时使用中文。
- 需要用户确认权限时，用中文说明准备执行什么、为什么需要执行、风险是什么。
"""


def with_chinese_interaction_instructions(prompt: str) -> str:
    body = str(prompt or "").strip()
    if CHINESE_INTERACTION_INSTRUCTIONS in body:
        return body
    if body:
        return f"{CHINESE_INTERACTION_INSTRUCTIONS}\n\n用户请求：\n{body}"
    return CHINESE_INTERACTION_INSTRUCTIONS
