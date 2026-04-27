"""Natural-language routing for project/task/service operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

from codepilot.storage import database as db
from codepilot.webapp.display_sort import sort_tasks_for_display


_DIGIT_MAP = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_UNIT_MAP = {"十": 10, "百": 100, "千": 1000}
_CHINESE_NUMBER_CHARS = "".join(sorted(set(_DIGIT_MAP) | set(_UNIT_MAP)))
_ACTION_NUMBER_RE = re.compile(rf"[0-9{_CHINESE_NUMBER_CHARS}][0-9{_CHINESE_NUMBER_CHARS}\s,，、/和]*")
_QUESTION_WORDS = (
    "怎么",
    "如何",
    "为什么",
    "为啥",
    "什么",
    "多少",
    "哪里",
    "哪个",
    "是否",
    "是不是",
    "有没有",
    "进度",
    "状态",
)
_REQUIREMENT_WORDS = (
    "帮我",
    "做",
    "实现",
    "修复",
    "修改",
    "新增",
    "添加",
    "优化",
    "重构",
    "接入",
    "处理",
)


@dataclass(frozen=True)
class CommandOption:
    command: str
    label: str


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _is_ascii_project_name(name: str) -> bool:
    return bool(name) and all(ord(char) < 128 for char in name)


def _project_in_text(text: str, project_name: str) -> bool:
    if not project_name:
        return False
    if _is_ascii_project_name(project_name):
        pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(project_name)}(?![A-Za-z0-9_.-])"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    return project_name in text


def _listed_projects() -> list[dict[str, Any]]:
    db.init_db()
    return db.list_projects()


def _project_names() -> list[str]:
    return [str(item.get("name") or "") for item in _listed_projects()]


def _resolve_default_project(active_project: str = "") -> str:
    candidate = str(active_project or "").strip()
    if candidate and db.get_project(candidate):
        return candidate
    projects = _listed_projects()
    if len(projects) == 1:
        return str(projects[0].get("name") or "")
    return ""


def _extract_projects(text: str, *, active_project: str = "") -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()
    for name in sorted(_project_names(), key=len, reverse=True):
        if not name or name in seen:
            continue
        if _project_in_text(text, name):
            seen.add(name)
            matches.append(name)
    if not matches and _contains_any(text, ("当前项目", "这个项目", "本项目")):
        default_project = _resolve_default_project(active_project)
        if default_project:
            matches.append(default_project)
    return matches


def _parse_chinese_number(token: str) -> int | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    normalized = raw.replace("两", "二").replace("〇", "零")
    if all(char in _DIGIT_MAP for char in normalized):
        return int("".join(str(_DIGIT_MAP[char]) for char in normalized))

    total = 0
    current = 0
    number = 0
    for char in normalized:
        if char in _DIGIT_MAP:
            number = _DIGIT_MAP[char]
            continue
        unit = _UNIT_MAP.get(char)
        if not unit:
            return None
        if number == 0:
            number = 1
        current += number * unit
        number = 0
    total = current + number
    return total if total > 0 else None


def _parse_id_sequence(raw: str) -> list[int]:
    tokens = [
        token.strip()
        for token in re.split(r"[\s,，、/和]+", str(raw or "").strip())
        if token.strip()
    ]
    ids: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        value = _parse_chinese_number(token)
        if value is None or value <= 0 or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    return ids


def _extract_task_ids(text: str) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()

    for token in re.findall(r"#?(\d+)", text):
        value = int(token)
        if value > 0 and value not in seen:
            seen.add(value)
            ids.append(value)

    for pattern in (
        rf"任务\s*({_ACTION_NUMBER_RE.pattern})",
        rf"({_ACTION_NUMBER_RE.pattern})\s*(?:号)?任务",
    ):
        for raw in re.findall(pattern, text):
            for value in _parse_id_sequence(raw):
                if value not in seen:
                    seen.add(value)
                    ids.append(value)
    return ids


def _recent_tasks(*, project: str = "") -> list[dict[str, Any]]:
    tasks = db.list_tasks(project=project or None)
    visible = [task for task in sort_tasks_for_display(tasks) if str(task.get("status") or "") != "archived"]
    return visible


def _task_command_options(
    action: str,
    *,
    project: str = "",
    limit: int = 4,
) -> list[CommandOption]:
    status_filters = {
        "stop": {"in_progress"},
        "retry": {"failed", "cancelled", "backlog"},
        "cancel": {"backlog", "failed"},
        "archive": {"done"},
        "delete": {"backlog", "cancelled", "done", "archived"},
        "detail": {"backlog", "in_progress", "done", "failed", "cancelled"},
        "logs": {"in_progress", "done", "failed", "cancelled", "backlog"},
    }
    allowed = status_filters.get(action) or set()
    options: list[CommandOption] = []
    for task in _recent_tasks(project=project):
        status = str(task.get("status") or "")
        if allowed and status not in allowed:
            continue
        task_id = int(task["id"])
        title = str(task.get("title") or "").strip() or f"任务 #{task_id}"
        if action in {"detail", "logs"}:
            label = f"{'查看' if action == 'detail' else '查看日志'} #{task_id} {title}"
        elif action == "retry":
            label = f"重试 #{task_id} {title}"
        elif action == "stop":
            label = f"停止 #{task_id} {title}"
        elif action == "cancel":
            label = f"取消 #{task_id} {title}"
        elif action == "archive":
            label = f"归档 #{task_id} {title}"
        else:
            label = f"删除 #{task_id} {title}"
        options.append(CommandOption(command=f"{action} {task_id}", label=label))
        if len(options) >= max(1, int(limit or 1)):
            break
    return options


def _project_options(
    command_prefix: str,
    *,
    active_project: str = "",
    label_prefix: str = "",
    limit: int = 4,
) -> list[CommandOption]:
    options: list[CommandOption] = []
    default_project = _resolve_default_project(active_project)
    for project in _listed_projects()[: max(1, int(limit or 1))]:
        name = str(project.get("name") or "")
        if not name:
            continue
        suffix = " (当前)" if default_project and name == default_project else ""
        label = f"{label_prefix}{name}{suffix}".strip()
        options.append(CommandOption(command=f"{command_prefix} {name}".strip(), label=label))
    return options


def _project_target(
    text: str,
    *,
    active_project: str = "",
) -> tuple[str, list[str]]:
    matches = _extract_projects(text, active_project=active_project)
    if len(matches) == 1:
        return matches[0], matches
    if len(matches) > 1:
        return "", matches
    default_project = _resolve_default_project(active_project)
    return default_project, matches


def _build_result(status: str, **payload: Any) -> dict[str, Any]:
    result = {"status": status}
    result.update(payload)
    return result


def _format_options(message: str, options: list[CommandOption]) -> dict[str, Any]:
    return _build_result(
        "options",
        message=message,
        options=[asdict(option) for option in options],
    )


def pick_command_option(text: str, options: list[dict[str, Any]] | list[CommandOption]) -> dict[str, Any] | None:
    raw = _normalize_text(text)
    if not raw:
        return None
    match = re.fullmatch(r"(\d+)", raw)
    if not match:
        return None
    index = int(match.group(1)) - 1
    normalized_options = [
        asdict(option) if isinstance(option, CommandOption) else dict(option)
        for option in (options or [])
    ]
    if index < 0 or index >= len(normalized_options):
        return None
    return normalized_options[index]


def format_numbered_options(message: str, options: list[dict[str, Any]] | list[CommandOption]) -> str:
    normalized_options = [
        asdict(option) if isinstance(option, CommandOption) else dict(option)
        for option in (options or [])
    ]
    lines = [message.strip()]
    for idx, option in enumerate(normalized_options, 1):
        lines.append(f"{idx}. {str(option.get('label') or option.get('command') or '').strip()}")
    lines.append("回复数字继续，例如 1")
    return "\n".join(line for line in lines if line)


def resolve_natural_language_command(text: str, *, active_project: str = "") -> dict[str, Any]:
    content = _normalize_text(text)
    if not content:
        return _build_result("no_match")

    lowered = content.lower()
    project_name, project_matches = _project_target(content, active_project=active_project)
    task_ids = _extract_task_ids(content)

    if _contains_any(content, ("项目列表", "项目清单", "有哪些项目", "所有项目")):
        return _build_result("match", command="projects", label="查看项目清单")

    if _contains_any(content, ("全局状态", "整体状态", "全部状态", "所有项目状态")):
        return _build_result("match", command="global", label="查看全局状态")

    if _contains_any(content, ("切换到", "切到", "进入", "使用")) and "项目" in content:
        if project_name:
            return _build_result("match", command=f"use {project_name}", label=f"切换到项目 {project_name}")
        options = _project_options("use", active_project=active_project, label_prefix="切换到项目 ")
        if options:
            return _format_options("你是想切换到哪个项目？", options)

    if _contains_any(content, ("巡检", "检查服务")):
        action = "status"
        if _contains_any(content, ("开始", "启动", "开启", "运行")):
            action = "start"
        elif _contains_any(content, ("停止", "关闭", "停掉", "结束")):
            action = "stop"
        if project_name:
            return _build_result("match", command=f"inspect {action} {project_name}", label=f"{action} 巡检服务")
        if len(project_matches) > 1:
            options = _project_options(f"inspect {action}", active_project=active_project, label_prefix="巡检项目 ")
            return _format_options("你是想操作哪个项目的巡检服务？", options)
        default_project = _resolve_default_project(active_project)
        if default_project:
            return _build_result("match", command=f"inspect {action} {default_project}", label=f"{action} 巡检服务")
        options = _project_options(f"inspect {action}", active_project=active_project, label_prefix="巡检项目 ")
        if options:
            return _format_options("你是想操作哪个项目的巡检服务？", options)

    if _contains_any(content, ("任务执行", "任务运行", "任务轮询", "执行服务", "运行服务", "任务级运行")):
        action = "status"
        if _contains_any(content, ("开始", "启动", "开启", "运行")):
            action = "start"
        elif _contains_any(content, ("停止", "关闭", "停掉", "结束")):
            action = "stop"
        if project_name:
            return _build_result("match", command=f"daemon {action} {project_name}", label=f"{action} 任务执行服务")
        if len(project_matches) > 1:
            options = _project_options(f"daemon {action}", active_project=active_project, label_prefix="任务执行服务 ")
            return _format_options("你是想操作哪个项目的任务执行服务？", options)
        default_project = _resolve_default_project(active_project)
        if default_project:
            return _build_result("match", command=f"daemon {action} {default_project}", label=f"{action} 任务执行服务")
        options = _project_options(f"daemon {action}", active_project=active_project, label_prefix="任务执行服务 ")
        if options:
            return _format_options("你是想操作哪个项目的任务执行服务？", options)

    task_action_map = (
        ("logs", ("日志", "log", "输出")),
        ("detail", ("详情", "详细", "内容", "信息")),
        ("retry", ("重试", "重新执行", "再跑", "重新跑")),
        ("stop", ("停止", "终止", "停掉", "结束")),
        ("cancel", ("取消",)),
        ("archive", ("归档",)),
        ("delete", ("删除", "删掉", "移除")),
    )
    if "任务" in content or task_ids:
        for action, keywords in task_action_map:
            if not _contains_any(content, keywords):
                continue
            if task_ids:
                if action == "delete" and len(task_ids) > 1:
                    return _build_result(
                        "match",
                        command="delete " + " ".join(str(task_id) for task_id in task_ids),
                        label="批量删除任务",
                    )
                if action == "cancel" and len(task_ids) > 1:
                    return _build_result(
                        "match",
                        command="cancel " + " ".join(str(task_id) for task_id in task_ids),
                        label="批量取消任务",
                    )
                if action == "archive" and len(task_ids) > 1:
                    return _build_result(
                        "match",
                        command="archive " + " ".join(str(task_id) for task_id in task_ids),
                        label="批量归档任务",
                    )
                return _build_result(
                    "match",
                    command=f"{action} {task_ids[0]}",
                    label=f"{action} 任务 #{task_ids[0]}",
                )

            options = _task_command_options(action, project=project_name or _resolve_default_project(active_project))
            if options:
                return _format_options("我需要具体任务 ID，你是指下面哪一个？", options)

    if _contains_any(content, ("任务列表", "任务面板", "看任务", "任务情况", "任务状态", "做完了没有", "完成了没有")):
        if project_name:
            return _build_result("match", command=f"tasks {project_name}", label=f"查看 {project_name} 任务")
        if len(project_matches) > 1:
            options = _project_options("tasks", active_project=active_project, label_prefix="查看任务 ")
            return _format_options("你是想看哪个项目的任务？", options)
        default_project = _resolve_default_project(active_project)
        if default_project:
            return _build_result("match", command=f"tasks {default_project}", label=f"查看 {default_project} 任务")
        options = _project_options("tasks", active_project=active_project, label_prefix="查看任务 ")
        if options:
            return _format_options("你是想看哪个项目的任务？", options)

    if _contains_any(content, ("服务状态", "服务情况", "服务列表")):
        if project_name:
            return _build_result("match", command=f"services {project_name}", label=f"查看 {project_name} 服务状态")
        if len(project_matches) > 1:
            options = _project_options("services", active_project=active_project, label_prefix="查看服务 ")
            return _format_options("你是想看哪个项目的服务状态？", options)
        default_project = _resolve_default_project(active_project)
        if default_project:
            return _build_result("match", command=f"services {default_project}", label=f"查看 {default_project} 服务状态")

    if _contains_any(content, ("项目状态", "项目概览", "项目总览", "运行状态", "看状态", "查看状态", "状态")):
        if project_name:
            return _build_result("match", command=f"overview {project_name}", label=f"查看 {project_name} 项目状态")
        if len(project_matches) > 1:
            options = _project_options("overview", active_project=active_project, label_prefix="查看项目状态 ")
            return _format_options("你是想看哪个项目的状态？", options)
        default_project = _resolve_default_project(active_project)
        if default_project and _contains_any(content, ("当前项目", "这个项目", "本项目", "运行状态", "看状态", "查看状态")):
            return _build_result("match", command=f"overview {default_project}", label=f"查看 {default_project} 项目状态")

    return _build_result("no_match")


def infer_goal_from_text(text: str, *, active_project: str = "") -> dict[str, Any] | None:
    content = _normalize_text(text)
    if not content:
        return None
    projects = _extract_projects(content, active_project=active_project)
    if len(projects) > 1:
        options = [CommandOption(command=f"use {name}", label=f"切换到项目 {name}") for name in projects[:4]]
        return _format_options("提到了多个项目，先确认你要在哪个项目里操作：", options)
    project_name = projects[0] if projects else _resolve_default_project(active_project)
    if not project_name:
        return None
    if content.endswith("?") or content.endswith("？") or _contains_any(content, _QUESTION_WORDS):
        return {"project": project_name, "text": content, "intent_hint": "question"}
    if _contains_any(content, _REQUIREMENT_WORDS):
        return {"project": project_name, "text": content, "intent_hint": "requirement"}
    return None
