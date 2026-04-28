"""Runtime data lookup and local rendering helpers for direct questions."""

from __future__ import annotations

import json
from typing import Optional

from codepilot.ai_support.agent_support import runtime_command_name
from codepilot.ai_support.intent_rules import (
    _normalize_question_key,
    _question_likely_needs_runtime_data,
    _question_mentions_tool_usage,
)
from codepilot.ai_support.providers import API_PROVIDERS, CLI_PROVIDERS
from codepilot.gateway.types import GatewayCallOptions
from codepilot.storage import database as db


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


def _question_runtime_lookup_requests(plan: dict) -> list[dict]:
    raw_lookups = plan.get("lookups") if isinstance(plan.get("lookups"), list) else []
    requests: list[dict] = []
    for raw in raw_lookups[:4]:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or "").strip()
        if not tool:
            continue
        requests.append(
            {
                "tool": tool,
                "limit": _runtime_lookup_limit(raw.get("limit")),
                "status": str(raw.get("status") or "all").strip().lower(),
            }
        )
    return requests


def _runtime_lookup_limit(value: object) -> int:
    try:
        limit = int(value or 5)
    except (TypeError, ValueError):
        return 5
    return max(1, min(limit, 20))


def _project_identity(project: dict) -> dict:
    return {
        "name": str(project.get("name") or ""),
        "path": str(project.get("path") or ""),
    }


def _empty_runtime_lookup_result(scope: str, current_project: Optional[dict]) -> dict:
    return {
        "scope": scope,
        "current_project": _project_identity(current_project) if current_project else None,
        "projects": [],
        "lookups": [],
        "notes": [],
    }


def _runtime_lookup_target_projects(
    scope: str,
    *,
    current_project: Optional[dict],
    projects: list[dict],
    result: dict,
) -> list[dict]:
    if scope == "all":
        return projects
    if current_project:
        return [current_project]
    if scope == "current":
        result["notes"] = ["当前没有可确认的项目上下文。"]
    return []


def _task_lookup_item(task: dict, fields: tuple[str, ...]) -> dict:
    item = {
        "id": int(task.get("id") or 0),
        "title": str(task.get("title") or ""),
    }
    for field in fields:
        item[field] = str(task.get(field) or "")
    return item


def _task_lookup_items(tasks: list[dict], *, limit: int, fields: tuple[str, ...]) -> list[dict]:
    return [_task_lookup_item(task, fields) for task in tasks[:limit]]


def _task_stats_lookup_items(snapshots: list[dict]) -> list[dict]:
    return [{"project": snapshot["name"], "stats": snapshot["stats"]} for snapshot in snapshots]


def _task_list_lookup_items(snapshots: list[dict], *, status: str, limit: int) -> list[dict]:
    items = []
    for snapshot in snapshots:
        tasks = snapshot["tasks"]
        filtered = tasks if status == "all" else [task for task in tasks if str(task.get("status") or "") == status]
        items.append(
            {
                "project": snapshot["name"],
                "status": status,
                "items": _task_lookup_items(filtered, limit=limit, fields=("status", "priority")),
            }
        )
    return items


def _status_task_lookup_items(snapshots: list[dict], *, status: str, limit: int, fields: tuple[str, ...]) -> list[dict]:
    return [
        {
            "project": snapshot["name"],
            "items": _task_lookup_items(
                [task for task in snapshot["tasks"] if str(task.get("status") or "") == status],
                limit=limit,
                fields=fields,
            ),
        }
        for snapshot in snapshots
    ]


def _service_status_lookup_items(snapshots: list[dict]) -> list[dict]:
    return [{"project": snapshot["name"], "services": snapshot["services"]} for snapshot in snapshots]


def _execute_runtime_lookup_request(request: dict, target_projects: list[dict]) -> dict | None:
    tool = str(request.get("tool") or "")
    if tool == "project_list":
        return None

    snapshots = [_project_runtime_snapshot(project) for project in target_projects]
    limit = int(request.get("limit") or 5)
    status = str(request.get("status") or "all")

    if tool == "service_status":
        return {"tool": tool, "items": _service_status_lookup_items(snapshots)}
    if not target_projects:
        return None
    if tool == "task_stats":
        return {"tool": tool, "items": _task_stats_lookup_items(snapshots)}
    if tool == "task_list":
        return {"tool": tool, "items": _task_list_lookup_items(snapshots, status=status, limit=limit)}
    if tool == "running_tasks":
        return {
            "tool": tool,
            "items": _status_task_lookup_items(
                snapshots,
                status="in_progress",
                limit=limit,
                fields=("priority",),
            ),
        }
    if tool == "failed_tasks":
        return {
            "tool": tool,
            "items": _status_task_lookup_items(
                snapshots,
                status="failed",
                limit=limit,
                fields=("priority", "error_message"),
            ),
        }
    return None


def _execute_question_runtime_lookups(plan: dict, *, project_path: str = "") -> dict:
    scope = str(plan.get("project_scope") or "current").strip().lower()
    requests = _question_runtime_lookup_requests(plan)
    current_project = _resolve_question_project(project_path)
    projects = db.list_projects()
    result = _empty_runtime_lookup_result(scope, current_project)
    target_projects = _runtime_lookup_target_projects(
        scope,
        current_project=current_project,
        projects=projects,
        result=result,
    )

    if any(request["tool"] == "project_list" for request in requests):
        result["projects"] = [_project_identity(project) for project in projects]

    for request in requests:
        lookup_result = _execute_runtime_lookup_request(request, target_projects)
        if lookup_result:
            result["lookups"].append(lookup_result)

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


def _lookup_entry_items(entry: dict) -> list:
    return entry.get("items") if isinstance(entry.get("items"), list) else []


def _render_task_stats_lookup(question: str, items: list, *, project_name: str) -> list[str]:
    normalized_question = _normalize_question_key(question)
    include_breakdown = any(keyword in normalized_question for keyword in ("状态", "进度", "失败", "执行中"))
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        project = str(item.get("project") or project_name or "当前项目")
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        total = int(stats.get("total") or 0)
        done = int(stats.get("done") or 0)
        remaining = max(total - done, 0)
        lines.append(f"项目 `{project}` 共有 {total} 个任务，已完成 {done} 个，未完成 {remaining} 个。")
        if include_breakdown:
            lines.append(
                "当前 "
                f"backlog={int(stats.get('backlog') or 0)}，"
                f"in_progress={int(stats.get('in_progress') or 0)}，"
                f"failed={int(stats.get('failed') or 0)}，"
                f"cancelled={int(stats.get('cancelled') or 0)}。"
            )
    return lines


def _render_task_refs(tasks: list, *, limit: int, include_status: bool = False) -> str:
    refs = []
    for task in tasks[:limit]:
        if not isinstance(task, dict):
            continue
        ref = f"#{int(task.get('id') or 0)} {str(task.get('title') or '').strip()}"
        if include_status:
            ref += f"[{str(task.get('status') or '').strip()}]"
        refs.append(ref)
    return "；".join(refs)


def _render_status_task_lookup(items: list, *, label: str, empty_text: str) -> list[str]:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tasks = item.get("items") if isinstance(item.get("items"), list) else []
        summary = _render_task_refs(tasks, limit=5)
        lines.append(f"{label}：{summary}。" if summary else empty_text)
    return lines


def _render_task_list_lookup(items: list) -> list[str]:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tasks = item.get("items") if isinstance(item.get("items"), list) else []
        summary = _render_task_refs(tasks, limit=8, include_status=True)
        if summary:
            lines.append(f"任务列表：{summary}。")
    return lines


def _render_service_status_lookup(items: list) -> list[str]:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        services = item.get("services") if isinstance(item.get("services"), dict) else {}
        daemon = services.get("daemon") if isinstance(services.get("daemon"), dict) else {}
        inspect = services.get("inspect") if isinstance(services.get("inspect"), dict) else {}
        daemon_label = "运行中" if daemon.get("running") else "未运行"
        inspect_label = "运行中" if inspect.get("running") else "未运行"
        lines.append(f"任务轮询 {daemon_label}，巡检 {inspect_label}。")
    return lines


def _render_project_list_fallback(data: dict) -> str:
    projects = data.get("projects") if isinstance(data.get("projects"), list) else []
    names = "、".join(
        f"`{str(project.get('name') or '').strip()}`"
        for project in projects[:10]
        if isinstance(project, dict)
    )
    return f"当前已注册项目有 {len(projects)} 个：{names}。" if names else ""


def _render_runtime_lookup_entry(question: str, entry: dict, *, project_name: str) -> list[str]:
    tool = str(entry.get("tool") or "").strip()
    items = _lookup_entry_items(entry)
    if tool == "task_stats":
        return _render_task_stats_lookup(question, items, project_name=project_name)
    if tool == "running_tasks":
        return _render_status_task_lookup(items, label="当前执行中的任务有", empty_text="当前没有执行中的任务。")
    if tool == "failed_tasks":
        return _render_status_task_lookup(items, label="当前失败任务有", empty_text="当前没有失败任务。")
    if tool == "task_list":
        return _render_task_list_lookup(items)
    if tool == "service_status":
        return _render_service_status_lookup(items)
    return []


def _render_runtime_lookup_answer(question: str, bundle: dict) -> str:
    if not bundle:
        return ""
    data = bundle.get("data") if isinstance(bundle.get("data"), dict) else {}
    notes = data.get("notes") if isinstance(data.get("notes"), list) else []
    if notes:
        return str(notes[0]).strip()

    current_project = data.get("current_project") if isinstance(data.get("current_project"), dict) else {}
    project_name = str(current_project.get("name") or "")
    lookup_items = data.get("lookups") if isinstance(data.get("lookups"), list) else []
    lines = [
        line
        for entry in lookup_items
        if isinstance(entry, dict)
        for line in _render_runtime_lookup_entry(question, entry, project_name=project_name)
    ]
    if not lines:
        return _render_project_list_fallback(data)
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
