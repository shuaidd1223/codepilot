"""Intent classification and quick-answer helpers.

Split out from ai.py. Re-exported via `codepilot.ai`. The API / CLI
routing itself lives in :mod:`codepilot.ai_gateway`; this module only
owns the heuristic, the intent schema + prompt, and the thin wrappers
that call the gateway.
"""

from __future__ import annotations

import re
from typing import Optional

from codepilot.ai_support.agent_support import command_manifest, runtime_command_name
from codepilot.gateway.types import GatewayCallOptions
from codepilot.ai_support.providers import _collect_project_context
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


def _looks_like_codepilot_command(text: str) -> bool:
    """Return whether ``text`` explicitly looks like a CodePilot CLI command."""
    t = (text or "").strip()
    if not t:
        return False
    lower = t.lower()

    if lower.startswith("codepilot "):
        return True
    if lower == "codepilot":
        return True
    if re.match(r"^(status|logs|retry|stop|inspect)\b", lower):
        return True
    if re.match(r"^release\s+prepare\b", lower):
        return True

    command_keywords = (
        "查看状态", "看一下状态", "看看状态", "列出任务", "看看任务",
        "查看日志", "看日志", "重试任务", "停止任务", "跑一下巡检", "触发巡检",
        "发布", "打包", "构建二进制",
    )
    return any(kw in t for kw in command_keywords)


def _local_tool_question_answer(question: str) -> str:
    """Return a deterministic local answer for tool-self questions."""
    text = (question or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"\s+", "", text.lower())

    command_keywords = (
        "有哪些命令",
        "命令清单",
        "工具命令",
        "当前工具",
        "怎么用这个工具",
        "如何使用这个工具",
        "有哪些功能",
        "支持哪些命令",
    )
    if not any(keyword in normalized for keyword in command_keywords):
        return ""

    manifest = command_manifest(command_name=runtime_command_name())
    command_name = str(manifest.get("command_name") or "codepilot").strip()
    commands = list(manifest.get("commands") or [])
    workflows = list(manifest.get("workflows") or [])

    focus_names = [
        "goal",
        "status",
        "show",
        "logs",
        "stop",
        "retry",
        "run",
        "daemon",
        "inspect",
        "ui",
        "doctor",
        "binary_prepare",
    ]
    by_name = {str(item.get("name") or ""): item for item in commands if isinstance(item, dict)}
    picked = [by_name[name] for name in focus_names if name in by_name]
    if not picked:
        picked = [item for item in commands[:10] if isinstance(item, dict)]

    lines = ["当前工具常用命令有这些："]
    for item in picked:
        syntax = str(item.get("syntax") or "").strip()
        purpose = str(item.get("purpose") or "").strip()
        if syntax:
            lines.append(f"- `{syntax}`：{purpose}")

    if workflows:
        first = workflows[0] if isinstance(workflows[0], dict) else {}
        steps = first.get("steps") if isinstance(first.get("steps"), list) else []
        if steps:
            lines.append("")
            lines.append("最常见的用法是：")
            for step in steps[:4]:
                lines.append(f"- `{str(step).strip()}`")

    lines.append("")
    lines.append(f"想看完整机器可读命令清单，执行 `{command_name} ai manifest`。")
    lines.append(f"想看 AI 使用手册，执行 `{command_name} ai guide`。")
    return "\n".join(lines)


def _resolve_gateway_options(
    *,
    gateway_options: Optional[GatewayCallOptions],
    classifier_provider: str,
    classifier_model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    project_path: str,
    config_ref: str,
    planner: str,
    timeout: int,
) -> GatewayCallOptions:
    """Merge explicit kwargs with an optional shared gateway context object."""
    shared = gateway_options
    effective_timeout = timeout
    if shared is not None and shared.timeout:
        effective_timeout = int(shared.timeout)
    return GatewayCallOptions(
        classifier_provider=(shared.classifier_provider if shared else "") or classifier_provider,
        classifier_model=(shared.classifier_model if shared else "") or classifier_model,
        api_key=shared.api_key if shared and shared.api_key is not None else api_key,
        base_url=shared.base_url if shared and shared.base_url is not None else base_url,
        project_path=(shared.project_path if shared else "") or project_path,
        config_ref=(shared.config_ref if shared else "") or config_ref,
        planner=planner,
        timeout=effective_timeout,
    )


def classify_intent(
    text: str,
    project_path: str = "",
    classifier_provider: str = "",
    classifier_model: str = "",
    timeout: int = 30,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    config_ref: str = "",
    gateway_options: Optional[GatewayCallOptions] = None,
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

    from codepilot.gateway.service import call_structured_prompt

    shared_options = _resolve_gateway_options(
        gateway_options=gateway_options,
        classifier_provider=classifier_provider,
        classifier_model=classifier_model,
        api_key=api_key,
        base_url=base_url,
        project_path=project_path,
        config_ref=config_ref,
        planner="claude",
        timeout=timeout,
    )
    from codepilot.core import progress_bus

    with progress_bus.llm_context(stage="planner", label="意图分类"):
        response = call_structured_prompt(
            prompt=INTENT_PROMPT.format(text=text),
            schema=INTENT_SCHEMA,
            # Classification is latency-sensitive; keep claude as CLI fallback family.
            options=shared_options,
        )

    if response.ok and response.payload:
        intent = response.payload.get("intent")
        if intent in valid_intents:
            if intent == "command" and not _looks_like_codepilot_command(text):
                return {
                    "intent": "requirement",
                    "reason": "命令防误判兜底：输入不符合 codepilot 命令形态",
                    "source": "guardrail",
                }
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
    base_url: Optional[str] = None,
    history: list[dict] | None = None,
    config_ref: str = "",
    gateway_options: Optional[GatewayCallOptions] = None,
) -> str:
    """Answer a user question directly without creating a task.

    Routes through :mod:`codepilot.ai_gateway` so API / CLI fallback and
    key-resolution behaviour stay consistent with ``classify_intent``.
    """
    local_answer = _local_tool_question_answer(question)
    if local_answer:
        return local_answer

    context = _collect_project_context(project_path, query_text=question)
    history_block = ""
    if history:
        lines = []
        for turn in history[-10:]:  # 最多保留最近 10 轮
            lines.append(f"用户: {turn['user']}")
            if turn.get("assistant"):
                lines.append(f"助手: {turn['assistant'][:300]}")
        history_block = "\n## 对话历史\n" + "\n".join(lines) + "\n"
    prompt = (
        "你是当前项目的协作助手。请基于下面的项目上下文和对话历史，用简洁中文直接回答用户问题。"
        "如果上下文显示当前目录并非明确项目根，请先明确这一点，再给出下一步最短可执行建议。"
        "不要只回复“我不确定”。\n\n"
        f"## 项目上下文\n{context}\n{history_block}\n## 用户问题\n{question}"
    )

    from codepilot.gateway.service import call_text_prompt

    shared_options = _resolve_gateway_options(
        gateway_options=gateway_options,
        classifier_provider=provider_key,
        classifier_model=model_override,
        api_key=api_key,
        base_url=base_url,
        project_path=project_path,
        config_ref=config_ref,
        planner="claude",
        timeout=120,
    )
    from codepilot.core import progress_bus

    with progress_bus.llm_context(stage="system", label="问题回答"):
        response = call_text_prompt(
            prompt=prompt,
            options=shared_options,
        )
    return response.text if response.ok else ""

