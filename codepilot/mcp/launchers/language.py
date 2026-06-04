"""Shared launcher prompt rules for user-visible language."""

from __future__ import annotations


ENGLISH_INTERACTION_INSTRUCTIONS = """\
Follow these interaction rules:
- All user-facing answers, progress updates, clarification questions, permission request reasons, error explanations, and thinking/reasoning summaries must be written in English.
- If the interface displays thinking / reasoning / analysis content, output a concise English summary and avoid Chinese headings or sentences.
- Code identifiers, commands, file paths, API names, MCP tool names, and raw error codes may stay in their original form.
- When asking the user to confirm permissions, explain in English what will be done, why it is needed, and what risk it carries.

YOUR WORKFLOW PIPELINE - for ANY requirement or task the user gives you, prefer the ONE-CALL approach:
1. ALWAYS PREFER: codepilot_pipeline(requirement="<full requirement>") - this handles plan → execute → workflow advance in one call.
2. FALLBACK: codepilot_run_once → codepilot_workflow_next(auto=true) loop when you only need execution, not planning.
3. When user activates "task mode": you are a tool-only observer. Do NOT read/write/edit files directly. Use ONLY CodePilot MCP tools. Call codepilot_pipeline, monitor results, report back. Never implement code yourself in this mode.
4. Check codepilot_daemon_status. If not running, drain the backlog yourself with run_once — never wait for the user.
"""


CHINESE_INTERACTION_INSTRUCTIONS = """\
请遵守以下交互规则：
- 所有面向用户的回答、执行过程说明、澄清问题、权限申请理由、错误解释和思考/推理摘要都使用简体中文。
- 如果界面展示 thinking / reasoning / analysis 内容，只输出中文摘要；不要使用 `Thinking`、`Reasoning`、`Analysis`、`I need to...` 这类英文标题或句式。
- 代码标识符、命令、文件路径、API 名称、MCP tool 名称和原始错误码可以保留英文；解释这些内容时使用中文。
- 需要用户确认权限时，用中文说明准备执行什么、为什么需要执行、风险是什么。

你的工作流管道 - 对用户给出的任何需求或任务，优先使用一键方式：
1. 始终优先使用：codepilot_pipeline(requirement="<完整需求>")——一次调用完成 规划→执行→工作流推进 全流程。
2. 回退方案：当你只需要执行而不需要规划时，使用 codepilot_run_once → codepilot_workflow_next(auto=true) 循环。
3. 当用户激活 "task 模式" 时：你是纯工具观察者。不要直接读/写/编辑文件。只使用 CodePilot MCP 工具。调用 codepilot_pipeline，监控结果并报告。绝不要自己动手写代码。
4. 检查 codepilot_daemon_status。如果未运行，自行用 run_once 清空 backlog——绝不要等待用户提醒你。
"""


def interaction_instructions(language: str = "en") -> str:
    normalized = str(language or "en").strip().lower()
    return CHINESE_INTERACTION_INSTRUCTIONS if normalized in {"zh", "zh-cn", "chinese", "中文"} else ENGLISH_INTERACTION_INSTRUCTIONS


def with_interaction_instructions(prompt: str, *, language: str = "en") -> str:
    instructions = interaction_instructions(language)
    body = str(prompt or "").strip()
    if instructions in body:
        return body
    if body:
        label = "用户请求：" if instructions == CHINESE_INTERACTION_INSTRUCTIONS else "User request:"
        return f"{instructions}\n\n{label}\n{body}"
    return instructions


def with_chinese_interaction_instructions(prompt: str) -> str:
    return with_interaction_instructions(prompt, language="zh-CN")


OUTPUT_LANGUAGE_INSTRUCTIONS: dict[str, str] = {
    "zh-CN": "\n\n请使用简体中文输出所有自然语言内容。",
    "en": "",
}


def inject_output_language(prompt: str, *, output_language: str = "en") -> str:
    """当输入语言 ≠ 输出语言时，在 prompt 末尾注入输出语言指令。

    提示词文件本身不硬编码输出语言——输出语言由 agent_output_language 配置控制，
    通过此函数在运行时注入。input=en + output=zh-CN 时追加中文输出指令；
    input=zh-CN + output=en 时追加英文输出指令；相同时不注入。
    """
    instruction = OUTPUT_LANGUAGE_INSTRUCTIONS.get(output_language, "")
    if not instruction:
        return prompt
    body = str(prompt or "").strip()
    # 避免重复注入
    if instruction.strip() in body:
        return body
    return body + instruction
