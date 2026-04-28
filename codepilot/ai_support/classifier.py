"""Intent classification and quick-answer helpers.

Split out from ai.py. Re-exported via `codepilot.ai`. The API / CLI
routing itself lives in :mod:`codepilot.ai_gateway`; this module only
owns the heuristic, the intent schema + prompt, and the thin wrappers
that call the gateway.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from codepilot.ai_support.agent_support import command_manifest, runtime_command_name
from codepilot.gateway.types import GatewayCallOptions
from codepilot.ai_support.providers import API_PROVIDERS, CLI_PROVIDERS, _collect_project_context
from codepilot.prompts import load_prompt as _load_prompt
from codepilot.storage import database as db

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

QUESTION_LOOKUP_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "use_local_data": {"type": "boolean"},
        "project_scope": {
            "type": "string",
            "enum": ["current", "all", "none"],
        },
        "lookups": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": [
                            "project_list",
                            "task_stats",
                            "task_list",
                            "running_tasks",
                            "failed_tasks",
                            "service_status",
                        ],
                    },
                    "status": {
                        "type": "string",
                        "enum": ["all", "backlog", "in_progress", "done", "failed", "cancelled"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "reason": {"type": "string"},
                },
                "required": ["tool"],
                "additionalProperties": False,
            },
        },
        "answer_focus": {"type": "string"},
    },
    "required": ["use_local_data", "project_scope", "lookups"],
    "additionalProperties": False,
}


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


def _resolve_question_project(project_path: str = "") -> Optional[dict]:
    project = db.find_project_by_path(project_path) if project_path else None
    if project:
        return project
    projects = db.list_projects()
    if len(projects) == 1:
        return projects[0]
    return None


def _runtime_service_snapshot(service: str, scope: str) -> dict:
    state = db.get_service_state(service, scope) or {}
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    return {
        "service": service,
        "scope": scope,
        "running": str(state.get("status") or "").strip().lower() == "running",
        "status": str(state.get("status") or "stopped"),
        "pid": int(state.get("pid") or 0),
        "heartbeat_at": str(state.get("heartbeat_at") or ""),
        "updated_at": str(state.get("updated_at") or ""),
        "meta": meta,
    }


def _project_runtime_snapshot(project: dict) -> dict:
    project_name = str(project.get("name") or "")
    project_path = str(project.get("path") or "")
    tasks = sorted(
        db.list_tasks(project=project_name),
        key=lambda item: int(item.get("id") or 0),
        reverse=True,
    )
    return {
        "name": project_name,
        "path": project_path,
        "stats": db.get_task_stats(project_name),
        "tasks": tasks,
        "services": {
            "daemon": _runtime_service_snapshot("daemon", project_name),
            "inspect": _runtime_service_snapshot("inspect", project_name),
        },
    }


def _question_lookup_plan_prompt(question: str, *, has_current_project: bool, project_name: str = "") -> str:
    current = f"当前项目可用：{project_name}" if has_current_project and project_name else "当前项目未知"
    return (
        "你是 CodePilot 的问答取数规划器。CodePilot 本身是一个全自动工作流智能体，"
        "你需要像智能体一样先理解问题，再决定是否读取本地工作流数据。"
        "只可使用给定工具，不要虚构工具，不要生成最终答案。"
        "如果问题偏概念、经验、解释，不需要取数时返回 use_local_data=false。"
        "如果问题涉及任务数量、完成度、项目状态、失败任务、服务运行状态、项目列表，就优先取数。"
        "\n\n"
        "可用工具：\n"
        "- project_list: 列出已注册项目\n"
        "- task_stats: 当前项目任务统计\n"
        "- task_list: 当前项目任务列表，可配 status 和 limit\n"
        "- running_tasks: 当前项目执行中的任务\n"
        "- failed_tasks: 当前项目失败任务\n"
        "- service_status: 当前项目 daemon/inspect 运行状态\n"
        "\n"
        f"{current}\n"
        f"用户问题：{question}\n"
        "输出要求：\n"
        "- 最多选择 4 个 lookups\n"
        "- 当前项目问题优先 project_scope=current\n"
        "- 全局比较或枚举问题再用 project_scope=all\n"
    )


def _add_question_lookup(plan: list[dict], tool: str, *, status: str | None = None, limit: int | None = None) -> None:
    if any(str(item.get("tool") or "") == tool for item in plan):
        return
    entry: dict[str, object] = {"tool": tool}
    if status:
        entry["status"] = status
    if limit is not None:
        entry["limit"] = int(limit)
    plan.append(entry)


def _heuristic_question_runtime_plan(question: str, *, project_path: str = "") -> dict:
    normalized = _normalize_question_key(question)
    lookups: list[dict] = []
    scope = "current"

    if any(keyword in normalized for keyword in ("所有项目", "全部项目", "各项目", "每个项目")):
        scope = "all"

    if any(keyword in normalized for keyword in ("有哪些项目", "项目列表", "项目清单", "注册项目")):
        scope = "all"
        _add_question_lookup(lookups, "project_list")

    if any(keyword in normalized for keyword in ("轮询", "巡检", "服务", "daemon", "inspect")):
        _add_question_lookup(lookups, "service_status")

    if "任务" in normalized or any(keyword in normalized for keyword in ("完成", "失败", "进度", "状态", "多少", "几个", "统计")):
        _add_question_lookup(lookups, "task_stats")

    if any(keyword in normalized for keyword in ("执行中", "运行中", "在跑", "进行中")):
        _add_question_lookup(lookups, "running_tasks", limit=8)

    if any(keyword in normalized for keyword in ("失败任务", "失败的任务", "报错任务", "异常任务", "失败", "报错", "异常")):
        _add_question_lookup(lookups, "failed_tasks", limit=8)

    wants_task_list = any(
        keyword in normalized
        for keyword in ("任务列表", "任务清单", "有哪些任务", "哪些任务", "列出任务", "看看任务", "最近任务")
    )
    if wants_task_list:
        task_status = "all"
        if any(keyword in normalized for keyword in ("已完成", "完成的", "done")):
            task_status = "done"
        elif any(keyword in normalized for keyword in ("执行中", "运行中", "在跑", "进行中")):
            task_status = "in_progress"
        elif any(keyword in normalized for keyword in ("失败", "报错", "异常")):
            task_status = "failed"
        elif any(keyword in normalized for keyword in ("待办", "未完成", "未做", "backlog")):
            task_status = "backlog"
        _add_question_lookup(lookups, "task_list", status=task_status, limit=10)

    if not lookups and _question_likely_needs_runtime_data(question):
        _add_question_lookup(lookups, "task_stats")

    return {
        "use_local_data": bool(lookups),
        "project_scope": scope if lookups else "none",
        "lookups": lookups[:4],
        "answer_focus": "",
        "source": "heuristic",
    }


def _plan_question_runtime_lookups(
    question: str,
    *,
    options: GatewayCallOptions,
    project_path: str = "",
) -> dict:
    from codepilot.gateway.service import call_structured_prompt
    from codepilot.core import progress_bus

    project = _resolve_question_project(project_path)
    prompt = _question_lookup_plan_prompt(
        question,
        has_current_project=project is not None,
        project_name=str(project.get("name") or "") if project else "",
    )
    with progress_bus.llm_context(stage="system", label="问题取数规划"):
        response = call_structured_prompt(
            prompt=prompt,
            schema=QUESTION_LOOKUP_PLAN_SCHEMA,
            options=options,
        )
    if response.ok and isinstance(response.payload, dict):
        return response.payload
    return {}


def _resolve_question_runtime_plan(
    question: str,
    *,
    options: GatewayCallOptions,
    project_path: str = "",
    allow_model_planner: bool,
) -> dict:
    if allow_model_planner:
        plan = _plan_question_runtime_lookups(question, options=options, project_path=project_path)
        if plan:
            return plan
    if not _question_likely_needs_runtime_data(question):
        return {}
    return _heuristic_question_runtime_plan(question, project_path=project_path)


def _execute_question_runtime_lookups(plan: dict, *, project_path: str = "") -> dict:
    lookups = plan.get("lookups") if isinstance(plan.get("lookups"), list) else []
    scope = str(plan.get("project_scope") or "current").strip().lower()
    current_project = _resolve_question_project(project_path)
    projects = db.list_projects()

    result: dict[str, object] = {
        "scope": scope,
        "current_project": {
            "name": str(current_project.get("name") or ""),
            "path": str(current_project.get("path") or ""),
        } if current_project else None,
        "projects": [],
        "lookups": [],
        "notes": [],
    }

    if scope == "all":
        target_projects = projects
    elif current_project:
        target_projects = [current_project]
    else:
        target_projects = []
        if scope == "current":
            result["notes"] = ["当前没有可确认的项目上下文。"]

    if any(str(item.get("tool") or "") == "project_list" for item in lookups):
        result["projects"] = [
            {
                "name": str(project.get("name") or ""),
                "path": str(project.get("path") or ""),
            }
            for project in projects
        ]

    for raw in lookups[:4]:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or "").strip()
        limit = int(raw.get("limit") or 5)
        status = str(raw.get("status") or "all").strip().lower()

        if tool == "project_list":
            continue

        if tool == "service_status":
            payload = []
            for project in target_projects:
                snapshot = _project_runtime_snapshot(project)
                payload.append(
                    {
                        "project": snapshot["name"],
                        "services": snapshot["services"],
                    }
                )
            result["lookups"].append({"tool": tool, "items": payload})
            continue

        if not target_projects:
            continue

        payload = []
        for project in target_projects:
            snapshot = _project_runtime_snapshot(project)
            tasks = snapshot["tasks"]
            if tool == "task_stats":
                payload.append(
                    {
                        "project": snapshot["name"],
                        "stats": snapshot["stats"],
                    }
                )
            elif tool == "task_list":
                filtered = tasks if status == "all" else [item for item in tasks if str(item.get("status") or "") == status]
                payload.append(
                    {
                        "project": snapshot["name"],
                        "status": status,
                        "items": [
                            {
                                "id": int(item.get("id") or 0),
                                "title": str(item.get("title") or ""),
                                "status": str(item.get("status") or ""),
                                "priority": str(item.get("priority") or ""),
                            }
                            for item in filtered[:limit]
                        ],
                    }
                )
            elif tool == "running_tasks":
                filtered = [item for item in tasks if str(item.get("status") or "") == "in_progress"]
                payload.append(
                    {
                        "project": snapshot["name"],
                        "items": [
                            {
                                "id": int(item.get("id") or 0),
                                "title": str(item.get("title") or ""),
                                "priority": str(item.get("priority") or ""),
                            }
                            for item in filtered[:limit]
                        ],
                    }
                )
            elif tool == "failed_tasks":
                filtered = [item for item in tasks if str(item.get("status") or "") == "failed"]
                payload.append(
                    {
                        "project": snapshot["name"],
                        "items": [
                            {
                                "id": int(item.get("id") or 0),
                                "title": str(item.get("title") or ""),
                                "priority": str(item.get("priority") or ""),
                                "error_message": str(item.get("error_message") or ""),
                            }
                            for item in filtered[:limit]
                        ],
                    }
                )
        result["lookups"].append({"tool": tool, "items": payload})

    return result


def _question_runtime_data_block(question: str, *, options: GatewayCallOptions, project_path: str = "") -> str:
    plan = _resolve_question_runtime_plan(
        question,
        options=options,
        project_path=project_path,
        allow_model_planner=True,
    )
    if not plan or not bool(plan.get("use_local_data")):
        return ""
    lookup_payload = _execute_question_runtime_lookups(plan, project_path=project_path)
    if not lookup_payload.get("projects") and not lookup_payload.get("lookups") and not lookup_payload.get("notes"):
        return ""
    return json.dumps(
        {
            "plan": plan,
            "data": lookup_payload,
        },
        ensure_ascii=False,
        indent=2,
    )


def _question_runtime_bundle(
    question: str,
    *,
    options: GatewayCallOptions,
    project_path: str = "",
    allow_model_planner: bool,
) -> dict:
    plan = _resolve_question_runtime_plan(
        question,
        options=options,
        project_path=project_path,
        allow_model_planner=allow_model_planner,
    )
    if not plan or not bool(plan.get("use_local_data")):
        return {}
    data = _execute_question_runtime_lookups(plan, project_path=project_path)
    if not data.get("projects") and not data.get("lookups") and not data.get("notes"):
        return {}
    return {"plan": plan, "data": data}


def _question_runtime_bundle_json(bundle: dict) -> str:
    if not bundle:
        return ""
    return json.dumps(bundle, ensure_ascii=False, indent=2)


def _render_runtime_lookup_answer(question: str, bundle: dict) -> str:
    if not bundle:
        return ""
    data = bundle.get("data") if isinstance(bundle.get("data"), dict) else {}
    notes = data.get("notes") if isinstance(data.get("notes"), list) else []
    lookup_items = data.get("lookups") if isinstance(data.get("lookups"), list) else []
    current_project = data.get("current_project") if isinstance(data.get("current_project"), dict) else {}
    project_name = str(current_project.get("name") or "")
    lines: list[str] = []

    if notes:
        return str(notes[0]).strip()

    for entry in lookup_items:
        if not isinstance(entry, dict):
            continue
        tool = str(entry.get("tool") or "").strip()
        items = entry.get("items") if isinstance(entry.get("items"), list) else []
        if tool == "task_stats":
            for item in items:
                if not isinstance(item, dict):
                    continue
                project = str(item.get("project") or project_name or "当前项目")
                stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
                total = int(stats.get("total") or 0)
                done = int(stats.get("done") or 0)
                in_progress = int(stats.get("in_progress") or 0)
                failed = int(stats.get("failed") or 0)
                backlog = int(stats.get("backlog") or 0)
                cancelled = int(stats.get("cancelled") or 0)
                remaining = max(total - done, 0)
                lines.append(
                    f"项目 `{project}` 共有 {total} 个任务，已完成 {done} 个，未完成 {remaining} 个。"
                )
                if any(keyword in _normalize_question_key(question) for keyword in ("状态", "进度", "失败", "执行中")):
                    lines.append(
                        f"当前 backlog={backlog}，in_progress={in_progress}，failed={failed}，cancelled={cancelled}。"
                    )
        elif tool == "running_tasks":
            for item in items:
                if not isinstance(item, dict):
                    continue
                running = item.get("items") if isinstance(item.get("items"), list) else []
                if running:
                    summary = "；".join(
                        f"#{int(task.get('id') or 0)} {str(task.get('title') or '').strip()}"
                        for task in running[:5]
                        if isinstance(task, dict)
                    )
                    lines.append(f"当前执行中的任务有：{summary}。")
                else:
                    lines.append("当前没有执行中的任务。")
        elif tool == "failed_tasks":
            for item in items:
                if not isinstance(item, dict):
                    continue
                failed_items = item.get("items") if isinstance(item.get("items"), list) else []
                if failed_items:
                    summary = "；".join(
                        f"#{int(task.get('id') or 0)} {str(task.get('title') or '').strip()}"
                        for task in failed_items[:5]
                        if isinstance(task, dict)
                    )
                    lines.append(f"当前失败任务有：{summary}。")
                else:
                    lines.append("当前没有失败任务。")
        elif tool == "task_list":
            for item in items:
                if not isinstance(item, dict):
                    continue
                task_items = item.get("items") if isinstance(item.get("items"), list) else []
                if task_items:
                    summary = "；".join(
                        f"#{int(task.get('id') or 0)} {str(task.get('title') or '').strip()}[{str(task.get('status') or '').strip()}]"
                        for task in task_items[:8]
                        if isinstance(task, dict)
                    )
                    lines.append(f"任务列表：{summary}。")
        elif tool == "service_status":
            for item in items:
                if not isinstance(item, dict):
                    continue
                services = item.get("services") if isinstance(item.get("services"), dict) else {}
                daemon = services.get("daemon") if isinstance(services.get("daemon"), dict) else {}
                inspect = services.get("inspect") if isinstance(services.get("inspect"), dict) else {}
                daemon_label = "运行中" if daemon.get("running") else "未运行"
                inspect_label = "运行中" if inspect.get("running") else "未运行"
                lines.append(f"任务轮询 {daemon_label}，巡检 {inspect_label}。")

    if not lines:
        projects = data.get("projects") if isinstance(data.get("projects"), list) else []
        if projects:
            names = "、".join(
                f"`{str(project.get('name') or '').strip()}`"
                for project in projects[:10]
                if isinstance(project, dict)
            )
            if names:
                return f"当前已注册项目有 {len(projects)} 个：{names}。"
        return ""

    return "\n".join(dict.fromkeys(line for line in lines if line.strip()))


def _question_prompt_header(project_path: str = "") -> str:
    selected_project = _resolve_question_project(project_path)
    if selected_project:
        name = str(selected_project.get("name") or "")
        path = str(selected_project.get("path") or "")
        return (
            "你是 CodePilot，一个全自动工作流智能体。这里的“当前项目”指工具里已注册并已选中的项目，"
            "请像智能体一样主动利用可用数据回答，不要把问题转成目录定位问题。\n\n"
            f"## 当前选中项目\nname={name}\npath={path}\n"
        )
    return (
        "你是 CodePilot，一个全自动工作流智能体。当前没有明确的选中项目时，才可以提示用户补充项目上下文；"
        "否则请直接基于已有信息回答，不要只回复“我不确定”。\n\n"
    )


def _local_projects_answer() -> str:
    projects = db.list_projects()
    if not projects:
        return "当前还没有已注册项目。先执行 `codepilot init <path>` 注册项目。"
    lines = [f"当前已注册项目 {len(projects)} 个："]
    for project in projects[:10]:
        name = str(project.get("name") or "")
        path = str(project.get("path") or "")
        stats = db.get_task_stats(name)
        lines.append(
            f"- `{name}`：backlog={stats['backlog']} in_progress={stats['in_progress']} failed={stats['failed']} done={stats['done']}，路径 `{path}`"
        )
    return "\n".join(lines)


def _local_project_status_answer(project_path: str) -> str:
    project = db.find_project_by_path(project_path) if project_path else None
    if not project:
        projects = db.list_projects()
        if len(projects) == 1:
            project = projects[0]
    if not project:
        return "当前没有可确认的项目上下文。先执行 `codepilot init <path>` 注册项目，或在已注册项目目录下提问。"

    project_name = str(project.get("name") or "")
    stats = db.get_task_stats(project_name)
    tasks = db.list_tasks(project=project_name)
    running = next((task for task in tasks if str(task.get("status") or "") == "in_progress"), None)
    failed = next((task for task in tasks if str(task.get("status") or "") == "failed"), None)

    lines = [
        f"当前项目是 `{project_name}`。",
        f"任务状态：backlog={stats['backlog']} in_progress={stats['in_progress']} failed={stats['failed']} cancelled={stats['cancelled']} done={stats['done']} total={stats['total']}。",
    ]
    if running:
        lines.append(
            f"当前执行中任务：`#{int(running['id'])} {str(running.get('title') or '').strip()}`。"
        )
    elif failed:
        lines.append(
            f"最近需要关注的失败任务：`#{int(failed['id'])} {str(failed.get('title') or '').strip()}`。"
        )
    else:
        lines.append("当前没有执行中的任务。")
    lines.append(f"想看详细面板可执行 `codepilot status -p {project_name} -v`。")
    return "\n".join(lines)


def _local_tool_usage_answer() -> str:
    command = runtime_command_name()
    lines = [
        "这个工具最常见的使用方式是：",
        f"- 初始化项目：`{command} init .`",
        f'- 提交需求：`{command} "修复任务重试逻辑并补测试"`',
        f"- 查看状态：`{command} status -p <项目名> -v`",
        f"- 查看任务详情：`{command} task show <task_id>`",
        f"- 查看任务日志：`{command} task logs <task_id>`",
        f"- 停止任务：`{command} task stop <task_id>`",
        f"- 重试任务：`{command} task retry <task_id>`",
        f"- 完整命令清单：`{command} ai manifest`",
    ]
    return "\n".join(lines)


def _local_task_totals_answer(project_path: str) -> str:
    project = _resolve_question_project(project_path)
    if not project:
        return "当前没有可确认的项目上下文，暂时无法统计任务总数和完成数。"

    project_name = str(project.get("name") or "")
    stats = db.get_task_stats(project_name)
    done = int(stats.get("done") or 0)
    total = int(stats.get("total") or 0)
    remaining = max(total - done, 0)
    return (
        f"当前项目 `{project_name}` 一共有 {total} 个任务，"
        f"已完成 {done} 个，未完成 {remaining} 个。"
    )


def _local_general_question_answer(question: str, *, project_path: str = "") -> str:
    text = (question or "").strip()
    if not text:
        return ""
    if _question_mentions_tool_usage(text):
        return _local_tool_usage_answer()
    return ""


def _provider_has_remote_answer_capability(provider_key: str, api_key: Optional[str], base_url: Optional[str]) -> bool:
    provider_name = (provider_key or "").strip()
    if not provider_name:
        return False
    provider = API_PROVIDERS.get(provider_name)
    if provider is None:
        return False
    effective_base_url = str(base_url or provider.base_url or "").strip()
    if effective_base_url.startswith(("http://localhost", "http://127.0.0.1")):
        return True
    if api_key:
        return True
    try:
        return bool(provider.resolve_api_key())
    except Exception:
        return False


def _has_local_question_answer_agent() -> bool:
    for key in ("codex", "claude", "claude-node"):
        provider = CLI_PROVIDERS.get(key)
        if provider and provider.find_executable():
            return True
    return False


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
    has_remote_capability = _provider_has_remote_answer_capability(provider_key, api_key, base_url)
    has_local_agent = _has_local_question_answer_agent()
    runtime_bundle: dict = {}
    try:
        runtime_bundle = _question_runtime_bundle(
            question,
            options=shared_options,
            project_path=shared_options.project_path or project_path,
            allow_model_planner=has_remote_capability,
        )
    except Exception:
        runtime_bundle = {}

    local_runtime_answer = _render_runtime_lookup_answer(question, runtime_bundle)
    general_local_answer = _local_general_question_answer(
        question,
        project_path=shared_options.project_path or project_path,
    )
    if local_runtime_answer and not has_remote_capability and not has_local_agent:
        return local_runtime_answer
    if general_local_answer and not has_remote_capability and not has_local_agent:
        return general_local_answer
    from codepilot.core import progress_bus

    runtime_data_block = _question_runtime_bundle_json(runtime_bundle)
    runtime_data_section = (
        f"\n## 本地取数结果\n{runtime_data_block}\n"
        if runtime_data_block
        else ""
    )
    prompt_header = _question_prompt_header(shared_options.project_path or project_path)
    with progress_bus.llm_context(stage="system", label="问题回答"):
        response = call_text_prompt(
            prompt=(
                f"{prompt_header}"
                "请基于下面的项目上下文、必要时的本地取数结果和对话历史，用简洁中文直接回答用户问题。"
                "如果已经给了本地取数结果，优先使用这些确定数据，不要忽略。"
                "不要只回复“我不确定”。\n\n"
                f"## 项目上下文\n{context}\n"
                f"{history_block}"
                f"{runtime_data_section}"
                f"## 用户问题\n{question}"
            ),
            options=shared_options,
        )
    if response.ok and response.text:
        return response.text
    if local_runtime_answer:
        return local_runtime_answer
    return general_local_answer if general_local_answer else ""

