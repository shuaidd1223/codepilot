"""Intent schemas, prompts and cheap rule-based classifiers."""

from __future__ import annotations

import re
from typing import Optional

from codepilot.ai_support.agent_support import command_manifest, runtime_command_name
from codepilot.prompts import load_prompt as _load_prompt


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
    if _is_tool_manifest_question(t):
        return "question"
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
    if _looks_like_information_request(t):
        return "question"
    if t.endswith("吗") or t.endswith("呢") or t.endswith("吧？"):
        return "question"
    # 明确以实现/修改类诉求开头才提前判 requirement，避免把宽泛讨论直接判死。
    requirement_starts = (
        "帮我", "请帮我", "请你帮我",
        "把",
        "实现", "修复", "修改", "添加", "新增", "优化", "重构",
        "删除", "移除", "升级", "迁移", "部署", "接入",
    )
    for word in requirement_starts:
        if t.startswith(word):
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
    if not _is_tool_manifest_question(text):
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


def _question_mentions_project_status(text: str) -> bool:
    normalized = _normalize_question_key(text)
    questions = {
        "当前项目状态",
        "当前项目状态怎么样",
        "当前项目任务状态",
        "这个项目状态",
        "这个项目状态怎么样",
        "这个项目任务状态",
        "本项目状态",
        "本项目状态怎么样",
        "本项目任务状态",
        "当前项目做完了没有",
        "这个项目做完了没有",
        "本项目做完了没有",
    }
    return normalized in questions


def _question_mentions_projects(text: str) -> bool:
    normalized = _normalize_question_key(text)
    questions = {
        "当前有哪些项目",
        "有哪些项目",
        "项目列表",
        "项目清单",
        "当前项目列表",
        "已注册项目列表",
        "已注册项目清单",
    }
    return normalized in questions


def _question_mentions_tool_usage(text: str) -> bool:
    normalized = _normalize_question_key(text)
    questions = {
        "怎么用这个工具",
        "如何使用这个工具",
        "怎么使用这个工具",
        "这个工具怎么用",
        "如何开始使用这个工具",
        "这个工具如何开始使用",
    }
    return normalized in questions


def _question_mentions_task_totals(text: str) -> bool:
    normalized = _normalize_question_key(text)
    questions = {
        "当前有多少个任务完成了多少",
        "当前项目有多少个任务完成了多少",
        "这个项目有多少个任务完成了多少",
        "本项目有多少个任务完成了多少",
        "当前任务总数和完成数",
        "当前项目任务总数和完成数",
        "这个项目任务总数和完成数",
        "本项目任务总数和完成数",
        "当前项目任务数量和完成数量",
    }
    return normalized in questions


def _normalize_question_key(text: str) -> str:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    return re.sub(r"[，。！？；：,.!?;:、\"'“”‘’（）()【】\[\]《》<>]", "", normalized)


def _looks_like_information_request(text: str) -> bool:
    normalized = _normalize_question_key(text)
    if not normalized:
        return False
    interrogatives = (
        "多少", "几个", "哪些", "哪几个", "哪一些", "有没有", "是否",
        "是什么", "是什么情况", "怎么样", "什么状态", "什么进度",
        "什么能力", "做什么", "干什么", "干嘛", "啥意思", "什么意思",
        "支持什么", "支持哪些", "能做什么", "好吗",
        "完成了多少", "有多少任务", "多少任务", "任务数", "完成数",
        "有哪些任务", "哪些任务", "在跑", "运行中", "执行中",
        "失败任务", "项目列表", "有哪些项目",
    )
    return any(token in normalized for token in interrogatives)


def _is_tool_manifest_question(text: str) -> bool:
    normalized = _normalize_question_key(text)
    command_questions = {
        "当前工具有哪些命令",
        "这个工具有哪些命令",
        "工具有哪些命令",
        "命令清单",
        "工具命令清单",
        "当前命令清单",
        "支持哪些命令",
        "这个工具支持哪些命令",
    }
    return normalized in command_questions


def _question_likely_needs_runtime_data(question: str) -> bool:
    normalized = _normalize_question_key(question)
    data_keywords = (
        "任务",
        "项目",
        "状态",
        "进度",
        "完成",
        "失败",
        "运行",
        "轮询",
        "巡检",
        "服务",
        "多少",
        "几个",
        "统计",
        "列表",
    )
    return any(keyword in normalized for keyword in data_keywords)
