"""Intent classification and quick-answer helpers.

Split out from ai.py. Re-exported via `codepilot.ai`. The API / CLI
routing itself lives in :mod:`codepilot.ai_gateway`; this module only
owns the heuristic, the intent schema + prompt, and the thin wrappers
that call the gateway.
"""

from __future__ import annotations

import re
from typing import Optional

from codepilot.ai_providers import _collect_project_context
from codepilot.prompts import load_prompt as _load_prompt

# ═══════════════════════════════════════════════════════════════════════════════

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["question", "task", "requirement", "command"],
        },
        "reason": {"type": "string"},
    },
    "required": ["intent"],
    "additionalProperties": False,
}

# Template body lives in codepilot/prompts/intent.md.
INTENT_PROMPT = _load_prompt("intent")


def _heuristic_intent(text: str) -> Optional[str]:
    """Cheap rule-based pre-filter. Returns None if unsure."""
    t = text.strip()
    if not t:
        return None
    # 直接命中 codepilot 内建命令动词
    command_keywords = (
        "查看状态", "看一下状态", "看看状态", "列出任务", "看看任务",
        "查看日志", "看日志", "重试任务", "停止任务", "跑一下巡检", "触发巡检",
        "发布", "打包", "构建二进制",
    )
    for kw in command_keywords:
        if kw in t:
            return "command"
    # 以问号结尾 → question
    if t.endswith("?") or t.endswith("？"):
        return "question"
    # 常见疑问词开头
    question_starts = (
        "怎么", "如何", "为什么", "为啥", "什么是", "什么叫",
        "能不能", "可不可以", "是不是", "有没有", "哪里", "哪个",
        "解释", "说明", "介绍", "告诉我", "请问",
    )
    for word in question_starts:
        if t.startswith(word):
            return "question"
    # 句中含疑问词（"做什么的"、"是什么"、"有哪些"、"怎样"、"吗"结尾等）
    question_contains = (
        "是什么", "做什么", "有什么", "有哪些", "哪些", "怎样", "怎么样",
        "能做什么", "提供什么", "支持什么", "包含什么",
        "是干什么", "干什么的", "干嘛的", "用来做什么",
        "多少", "几个", "啥意思", "什么意思",
    )
    for word in question_contains:
        if word in t:
            return "question"
    if t.endswith("吗") or t.endswith("呢") or t.endswith("吧？"):
        return "question"
    # 含"帮我""实现""修复""添加""优化"等动词 → requirement
    requirement_verbs = (
        "帮我", "实现", "修复", "修改", "添加", "新增", "优化", "重构",
        "删除", "移除", "升级", "迁移", "部署", "接入",
    )
    for word in requirement_verbs:
        if word in t:
            return "requirement"
    return None


def classify_intent(
    text: str,
    project_path: str = "",
    classifier_provider: str = "",
    classifier_model: str = "",
    timeout: int = 30,
    api_key: Optional[str] = None,
) -> dict:
    """Classify a chat input as question / task / requirement.

    Strategy:
      1. Heuristic pre-filter (free, instant).
      2. Unified AI gateway: configured API provider, then local CLI fallback.
      3. On any failure, default to 'requirement' (preserves prior behavior).
    """
    text = text.strip()
    if not text:
        return {"intent": "requirement", "reason": "空输入", "source": "default"}

    guess = _heuristic_intent(text)
    if guess:
        return {"intent": guess, "reason": "启发式规则命中", "source": "heuristic"}

    valid_intents = {"question", "task", "requirement", "command"}

    from codepilot.ai_gateway import GatewayRequest, call_structured

    response = call_structured(
        GatewayRequest(
            prompt=INTENT_PROMPT.format(text=text),
            schema=INTENT_SCHEMA,
            classifier_provider=classifier_provider,
            classifier_model=classifier_model,
            api_key=api_key,
            project_path=project_path,
            planner="claude",  # classification is latency-sensitive; prefer claude
            timeout=timeout,
        )
    )

    if response.ok and response.payload:
        intent = response.payload.get("intent")
        if intent in valid_intents:
            return {
                "intent": intent,
                "reason": response.payload.get("reason", ""),
                "source": response.source,
            }

    return {
        "intent": "requirement",
        "reason": f"分类失败，默认当作需求处理（{response.error or '未知原因'}）",
        "source": "default",
    }


def answer_question_via_api(
    provider_key: str,
    question: str,
    project_path: str = "",
    model_override: str = "",
    api_key: Optional[str] = None,
    history: list[dict] | None = None,
) -> str:
    """Answer a user question directly without creating a task.

    Routes through :mod:`codepilot.ai_gateway` so API / CLI fallback and
    key-resolution behaviour stay consistent with ``classify_intent``.
    """
    context = _collect_project_context(project_path)
    history_block = ""
    if history:
        lines = []
        for turn in history[-10:]:  # 最多保留最近 10 轮
            lines.append(f"用户: {turn['user']}")
            if turn.get("assistant"):
                lines.append(f"助手: {turn['assistant'][:300]}")
        history_block = "\n## 对话历史\n" + "\n".join(lines) + "\n"
    prompt = (
        "你是当前项目的协作助手。请基于下面的项目上下文和对话历史，"
        "用简洁中文直接回答用户的问题。如果不确定，明确说不确定。\n\n"
        f"## 项目上下文\n{context}\n{history_block}\n## 用户问题\n{question}"
    )

    from codepilot.ai_gateway import GatewayRequest, call_text

    response = call_text(
        GatewayRequest(
            prompt=prompt,
            classifier_provider=provider_key,
            classifier_model=model_override,
            api_key=api_key,
            project_path=project_path,
            planner="claude",
            timeout=120,
        )
    )
    return response.text if response.ok else ""
