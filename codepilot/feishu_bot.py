"""Feishu long-connection bot command routing and card rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codepilot.core.config import load_config
from codepilot.storage import database as db
from codepilot.webapp.action_task_ops import project_service_action, stop_task_action
from codepilot.webapp.action_requirements import retry_task_action
from codepilot.webapp.payloads import dashboard_payload, task_detail_payload


@dataclass
class FeishuBotConfig:
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    node_command: str = "node"
    default_project: str = ""
    command_prefix: str = ""


def load_feishu_bot_config(config_path: Path | None = None) -> FeishuBotConfig:
    cfg = load_config(config_path)
    if cfg is None:
        return FeishuBotConfig()
    section = cfg.feishu_bot if isinstance(cfg.feishu_bot, dict) else {}
    return FeishuBotConfig(
        enabled=bool(cfg.feishu_bot_enabled or section.get("enabled", False)),
        app_id=str(cfg.feishu_app_id or section.get("app_id", "") or ""),
        app_secret=str(cfg.feishu_app_secret or section.get("app_secret", "") or ""),
        node_command=str(cfg.feishu_node_command or section.get("node_command", "node") or "node"),
        default_project=str(cfg.feishu_default_project or section.get("default_project", "") or ""),
        command_prefix=str(cfg.feishu_command_prefix or section.get("command_prefix", "") or "").strip(),
    )


def validate_feishu_bot_config(config_path: Path | None = None) -> list[str]:
    cfg = load_feishu_bot_config(config_path)
    problems: list[str] = []
    if not cfg.enabled:
        problems.append("[feishu_bot].enabled = true 未开启。")
    if not cfg.app_id:
        problems.append("[feishu_bot].app_id 为空。")
    if not cfg.app_secret:
        problems.append("[feishu_bot].app_secret 为空。")
    if not cfg.node_command:
        problems.append("[feishu_bot].node_command 为空。")
    return problems


def _status_label(status: str) -> str:
    return {
        "backlog": "待执行",
        "in_progress": "执行中",
        "done": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "archived": "已归档",
    }.get(str(status or ""), str(status or "-"))


def _status_emoji(status: str) -> str:
    return {
        "backlog": "⏳",
        "in_progress": "🚧",
        "done": "✅",
        "failed": "❌",
        "cancelled": "🛑",
        "archived": "📦",
    }.get(str(status or ""), "•")


def _card(title: str, blocks: list[str], *, template: str = "blue", note: str = "") -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for block in blocks:
        text = str(block or "").strip()
        if not text:
            continue
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": text}})
    if note:
        elements.append({"tag": "hr"})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title[:120]},
        },
        "elements": elements,
    }


def _reply_card(card: dict[str, Any]) -> dict[str, Any]:
    return {"type": "interactive", "card": card}


def _reply_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _help_note(prefix: str) -> str:
    lead = f"{prefix} " if prefix else ""
    return (
        f"命令示例: {lead}projects | {lead}tasks demo | {lead}detail 123 | "
        f"{lead}stop 123 | {lead}retry 123 | {lead}run demo | {lead}status demo"
    )


def build_help_card(*, prefix: str = "", error: str = "") -> dict[str, Any]:
    blocks = []
    if error:
        blocks.append(f"**未识别命令**\n{error}")
    blocks.append(
        "\n".join(
            [
                "**可用命令**",
                "• `projects`：查看已注册项目",
                "• `tasks <project>`：查看项目任务面板",
                "• `detail <task_id>`：查看任务详情",
                "• `stop <task_id>`：停止运行中的任务",
                "• `retry <task_id>`：重试任务并立即后台执行",
                "• `run <project>`：启动该项目的任务执行服务",
                "• `status <project>`：查看该项目任务执行服务状态",
            ]
        )
    )
    return _card("CodePilot 飞书命令", blocks, template="indigo", note=_help_note(prefix))


def _resolve_project(explicit: str, *, default_project: str = "") -> str:
    name = str(explicit or "").strip()
    if name:
        project = db.get_project(name)
        if not project:
            raise RuntimeError(f"项目 '{name}' 未注册。")
        return name
    default_name = str(default_project or "").strip()
    if default_name:
        project = db.get_project(default_name)
        if project:
            return default_name
    projects = db.list_projects()
    if len(projects) == 1:
        return str(projects[0]["name"])
    raise RuntimeError("请显式指定项目，例如 `tasks demo` 或先执行 `projects`。")


def build_projects_card(*, prefix: str = "", default_project: str = "") -> dict[str, Any]:
    db.init_db()
    projects = db.list_projects()
    if not projects:
        return _card(
            "CodePilot 项目",
            ["当前还没有已注册项目。先在本机运行 `codepilot init <path>` 注册项目。"],
            template="orange",
            note=_help_note(prefix),
        )
    lines: list[str] = []
    for project in projects[:12]:
        stats = db.get_task_stats(project["name"])
        label = f" `{project['name']}`"
        if project["name"] == default_project:
            label += " (默认)"
        lines.append(
            f"•{label}  backlog {stats.get('backlog', 0)} / "
            f"in_progress {stats.get('in_progress', 0)} / failed {stats.get('failed', 0)}"
        )
    return _card("CodePilot 项目", ["\n".join(lines)], template="blue", note=_help_note(prefix))


def build_tasks_card(project_name: str, *, prefix: str = "") -> dict[str, Any]:
    snapshot = dashboard_payload(project_name)
    project = next((item for item in snapshot["projects"] if item["name"] == project_name), None)
    if project is None:
        raise RuntimeError(f"项目 '{project_name}' 未注册。")
    tasks = snapshot.get("tasks", [])[:8]
    stats = project.get("stats") or {}
    blocks = [
        (
            f"**项目** `{project_name}`\n"
            f"backlog: **{stats.get('backlog', 0)}**  "
            f"in_progress: **{stats.get('in_progress', 0)}**  "
            f"failed: **{stats.get('failed', 0)}**  "
            f"done: **{stats.get('done', 0)}**"
        )
    ]
    if tasks:
        blocks.append(
            "\n".join(
                f"{_status_emoji(task['status'])} `#{task['id']}` **{task['title'][:60]}**  "
                f"`{_status_label(task['status'])}` / `{task['priority']}`"
                for task in tasks
            )
        )
    else:
        blocks.append("当前没有可展示的任务。")
    return _card(
        f"CodePilot 任务面板 · {project_name}",
        blocks,
        template="turquoise",
        note=_help_note(prefix),
    )


def build_task_card(task_id: int, *, prefix: str = "", title_prefix: str = "任务详情") -> dict[str, Any]:
    task = task_detail_payload(task_id)
    latest = str(task.get("latest") or task.get("delivery_record") or task.get("error_message") or "").strip()
    latest = latest[:240]
    blocks = [
        (
            f"**项目** `{task['project']}`\n"
            f"**状态** `{_status_label(task['status'])}`\n"
            f"**优先级** `{task['priority']}`\n"
            f"**Agent** `{task['agent']}`"
        ),
        f"**标题**\n{task['title']}",
    ]
    if latest:
        blocks.append(f"**最近输出**\n{latest}")
    return _card(
        f"{title_prefix} · #{task_id}",
        blocks,
        template="green" if task["status"] == "done" else "blue",
        note=_help_note(prefix),
    )


def build_service_card(project_name: str, result: dict[str, Any], *, prefix: str = "", title: str = "任务执行服务") -> dict[str, Any]:
    status = result.get("status") if isinstance(result.get("status"), dict) else result
    running = bool(status.get("running"))
    stopping = bool(status.get("stopping"))
    blocks = [
        (
            f"**项目** `{project_name}`\n"
            f"**服务状态** `{'运行中' if running else '未运行'}`\n"
            f"**PID** `{status.get('pid') or 0}`"
        )
    ]
    message = str(result.get("message") or "").strip()
    if message:
        blocks.append(f"**结果**\n{message}")
    if stopping:
        blocks.append("当前已收到停止请求，正在等待正在执行的任务收尾。")
    return _card(f"{title} · {project_name}", blocks, template="purple", note=_help_note(prefix))


def _normalize_command_text(text: str, prefix: str) -> str | None:
    content = " ".join(str(text or "").strip().split())
    if not content:
        return None
    if prefix:
        if not content.lower().startswith(prefix.lower()):
            return None
        content = content[len(prefix):].strip()
        if not content:
            return "help"
    return content


def _parse_task_id(token: str) -> int:
    try:
        task_id = int(str(token or "").strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("任务 ID 必须是整数。") from exc
    if task_id <= 0:
        raise RuntimeError("任务 ID 必须大于 0。")
    return task_id


def handle_command_text(text: str, *, config_path: Path | None = None) -> dict[str, Any]:
    db.init_db()
    cfg = load_feishu_bot_config(config_path)
    command_text = _normalize_command_text(text, cfg.command_prefix)
    if command_text is None:
        return {"type": "ignore"}
    parts = command_text.split()
    if not parts:
        return _reply_card(build_help_card(prefix=cfg.command_prefix))

    verb = parts[0].lower()
    if verb in {"help", "h", "?"}:
        return _reply_card(build_help_card(prefix=cfg.command_prefix))
    if verb in {"projects", "ls"}:
        return _reply_card(build_projects_card(prefix=cfg.command_prefix, default_project=cfg.default_project))
    if verb in {"tasks", "panel"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=cfg.default_project)
        return _reply_card(build_tasks_card(project_name, prefix=cfg.command_prefix))
    if verb in {"detail", "task", "show"}:
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `detail 123`。")
        return _reply_card(build_task_card(_parse_task_id(parts[1]), prefix=cfg.command_prefix))
    if verb == "stop":
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `stop 123`。")
        task_id = _parse_task_id(parts[1])
        stop_task_action(task_id)
        return _reply_card(build_task_card(task_id, prefix=cfg.command_prefix, title_prefix="停止请求已发送"))
    if verb == "retry":
        if len(parts) < 2:
            raise RuntimeError("请提供任务 ID，例如 `retry 123`。")
        task_id = _parse_task_id(parts[1])
        retry_task_action(task_id)
        return _reply_card(build_task_card(task_id, prefix=cfg.command_prefix, title_prefix="任务已重试"))
    if verb in {"run", "start"}:
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=cfg.default_project)
        result = project_service_action(project_name, "tasks", "start")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title="任务执行服务已启动"))
    if verb == "status":
        project_name = _resolve_project(parts[1] if len(parts) > 1 else "", default_project=cfg.default_project)
        result = project_service_action(project_name, "tasks", "status")
        return _reply_card(build_service_card(project_name, result, prefix=cfg.command_prefix, title="任务执行服务状态"))

    return _reply_card(build_help_card(prefix=cfg.command_prefix, error=f"`{command_text}`"))


def handle_event_payload(payload: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    try:
        return handle_command_text(str(payload.get("text") or ""), config_path=config_path)
    except Exception as exc:
        cfg = load_feishu_bot_config(config_path)
        return _reply_card(
            _card(
                "CodePilot 命令执行失败",
                [str(exc)],
                template="red",
                note=_help_note(cfg.command_prefix),
            )
        )
