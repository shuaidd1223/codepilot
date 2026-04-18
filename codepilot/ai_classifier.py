"""Intent classification and quick-answer helpers.

Split out from ai.py. Re-exported via `codepilot.ai`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Optional

from codepilot.ai_providers import (
    API_PROVIDERS,
    CLI_PROVIDERS,
    _ensure_claude_git_bash_env,
    _run_api_provider,
    resolve_cli_provider,
)

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

INTENT_PROMPT = """你是一个输入意图分类器。请把用户下面的一句话分到下列四类之一，并以 JSON 返回：

- question: 用户是在问问题、求解释或求建议，不需要你去改代码或建任务。
- task: 用户想做一件具体小事，一步就能完成，不需要拆分。
- requirement: 用户想做一个较大的需求，涉及多步或多模块，需要拆分成子任务。
- command: 用户想直接调 codepilot 自身的某个命令（查看状态、日志、重试、停止、巡检、发布等），不是对代码本身下需求。

只输出 JSON，字段：intent, reason（一句中文说明判断依据）。

用户输入：
{text}
"""


def _classify_via_api(
    provider: APIProvider,
    text: str,
    timeout: int = 30,
) -> dict:
    """Call an API provider with the intent classification prompt."""
    _ = timeout  # API clients have their own timeouts
    raw = _run_api_provider(provider, INTENT_PROMPT.format(text=text))
    # 宽松解析：可能带 ``` 或前缀
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start : end + 1]
    return json.loads(raw)


def _classify_via_codex(
    text: str,
    project_path: str = "",
    timeout: int = 30,
) -> dict:
    """Fallback: use local codex CLI with schema-constrained output."""
    # Late import to avoid circular import: ai.py re-exports from this module.
    # Using the canonical `codepilot.ai` attribute also lets tests monkeypatch
    # `codepilot.ai._run_codex_schema_prompt`.
    from codepilot import ai as _ai
    return _ai._run_codex_schema_prompt(
        INTENT_PROMPT.format(text=text),
        INTENT_SCHEMA,
        project_path=project_path,
        timeout=timeout,
    )


def _classify_via_claude_cli(
    text: str,
    project_path: str = "",
    timeout: int = 30,
) -> dict:
    """Use local claude CLI with --json-schema for intent classification."""
    provider = resolve_cli_provider("claude", project_path or None)
    exe = provider.find_executable()
    if not exe:
        raise RuntimeError("claude CLI 不可用")

    prompt = INTENT_PROMPT.format(text=text)
    cmd = [
        str(exe),
        "--output-format", "json",
        "--json-schema", json.dumps(INTENT_SCHEMA, ensure_ascii=False),
        "--dangerously-skip-permissions",
        "-p", prompt,
    ]
    result = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError("claude 分类失败")
    output = (result.stdout or "").strip()
    if not output:
        raise RuntimeError("claude 分类返回空内容")
    payload = json.loads(output)
    # claude --output-format json wraps result in envelope
    if isinstance(payload, dict) and "structured_output" in payload:
        return payload["structured_output"]
    return payload


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
      2. Configured API provider if available.
      3. Local codex CLI fallback.
      4. On any failure, default to 'requirement' (preserves current behavior).
    """
    text = text.strip()
    if not text:
        return {"intent": "requirement", "reason": "空输入", "source": "default"}

    guess = _heuristic_intent(text)
    if guess:
        return {"intent": guess, "reason": "启发式规则命中", "source": "heuristic"}

    # API path
    valid_intents = {"question", "task", "requirement", "command"}
    if classifier_provider and classifier_provider in API_PROVIDERS:
        provider = replace(API_PROVIDERS[classifier_provider])
        if classifier_model:
            provider.model = classifier_model
        if api_key:
            provider.api_key = api_key
        try:
            if not provider.requires_api_key() or provider.resolve_api_key():
                payload = _classify_via_api(provider, text, timeout=timeout)
                intent = payload.get("intent")
                if intent in valid_intents:
                    return {
                        "intent": intent,
                        "reason": payload.get("reason", ""),
                        "source": f"api:{classifier_provider}",
                    }
        except Exception as exc:
            # API 失败 → 继续尝试本地
            last_error = str(exc)
        else:
            last_error = ""
    else:
        last_error = ""

    # Local claude CLI fallback（比 codex 快）
    try:
        payload = _classify_via_claude_cli(text, project_path=project_path, timeout=timeout)
        intent = payload.get("intent")
        if intent in valid_intents:
            return {
                "intent": intent,
                "reason": payload.get("reason", ""),
                "source": "claude-cli",
            }
    except Exception:
        pass

    # Local codex fallback
    try:
        payload = _classify_via_codex(text, project_path=project_path, timeout=timeout)
        intent = payload.get("intent")
        if intent in valid_intents:
            return {
                "intent": intent,
                "reason": payload.get("reason", ""),
                "source": "codex",
            }
    except Exception as exc:
        last_error = str(exc)

    return {
        "intent": "requirement",
        "reason": f"分类失败，默认当作需求处理（{last_error or '未知原因'}）",
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
    """Answer a user question directly without creating a task."""
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
    if provider_key and provider_key in API_PROVIDERS:
        provider = replace(API_PROVIDERS[provider_key])
        if model_override:
            provider.model = model_override
        if api_key:
            provider.api_key = api_key
        return _run_api_provider(provider, prompt)
    # 无 API 时用本地 claude CLI 回答
    return _answer_via_local_cli(prompt, project_path=project_path)


def _answer_via_local_cli(prompt: str, project_path: str = "", timeout: int = 120) -> str:
    """Use claude or codex CLI to answer a question directly."""
    # 优先 claude
    for cli_name in ("claude", "codex"):
        try:
            provider = resolve_cli_provider(cli_name, project_path or None)
            exe = provider.find_executable()
        except Exception:
            continue
        if not exe:
            continue

        if cli_name == "codex":
            cmd = [str(exe), "exec", "--skip-git-repo-check", "--ephemeral",
                   "--dangerously-bypass-approvals-and-sandbox"]
            if project_path:
                cmd = [str(exe), "-C", project_path] + cmd[1:]
        else:
            cmd = [str(exe), "-p", "--output-format", "text", "--dangerously-skip-permissions"]

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            if result.returncode == 0 and (result.stdout or "").strip():
                return result.stdout.strip()
        except Exception:
            continue
    return ""
