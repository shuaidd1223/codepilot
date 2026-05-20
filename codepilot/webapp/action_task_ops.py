"""Project and task management actions for the Web UI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from codepilot.ai_support.agent_support import task_template_schema
from codepilot.commands.init import initialize_project
from codepilot.core.runtime import clear_task_runtime, stop_worktree_leftovers
from codepilot.core.task_template import (
    missing_task_template_sections,
    unreplaced_task_template_placeholders,
)
from codepilot.storage import database as db
from codepilot.webapp.action_state import _append_event
from codepilot.webapp.payloads import _task_payload


def create_project_action(path: str, *, name: str = "", no_config: bool = False) -> dict:
    db.init_db()
    raw_path = (path or "").strip().strip('"')
    if not raw_path:
        raise RuntimeError("工作目录不能为空。")
    resolved_path = Path(raw_path).expanduser().resolve()
    requested_name = (name or "").strip() or resolved_path.name
    if not requested_name:
        raise RuntimeError("项目名称不能为空。")

    existing_by_name = db.get_project(requested_name)
    existing_by_path = db.find_project_by_path(str(resolved_path))
    if existing_by_path and Path(existing_by_path["path"]).resolve() != resolved_path:
        existing_by_path = None
    if existing_by_name and Path(existing_by_name["path"]).resolve() != resolved_path:
        raise RuntimeError(
            f"项目 '{requested_name}' 已注册到 `{existing_by_name['path']}`，不能再绑定到 `{resolved_path}`。"
        )
    if existing_by_path and str(existing_by_path.get("name") or "").strip() != requested_name:
        raise RuntimeError(
            f"路径 `{resolved_path}` 已注册为项目 '{existing_by_path['name']}'，不能重复登记为 '{requested_name}'。"
        )

    result = initialize_project(resolved_path, requested_name, no_config=no_config)
    project = result["project"]
    config_file = result.get("config_file") or ""
    if not result["created"] and not no_config and config_file:
        project = db.register_project(
            project["name"],
            str(resolved_path),
            base_branch=str(project.get("base_branch") or "dev"),
            default_mode=str(project.get("default_mode") or "dual"),
            worktree_base=project.get("worktree_base"),
            config_file=config_file,
        )
        result["project"] = project
    action = "注册" if result["created"] else "更新"
    _append_event(f"{action}项目：{project['name']}", project=project["name"])
    return {
        "ok": True,
        "created": bool(result["created"]),
        "message": f"项目 '{project['name']}' 已{action}。",
        "project": project,
        "config_file": config_file,
    }


def delete_project_action(name: str) -> dict:
    db.init_db()
    project_name = (name or "").strip()
    if not project_name:
        raise RuntimeError("项目名称不能为空。")
    project = db.get_project(project_name)
    if not project:
        raise RuntimeError(f"项目 '{project_name}' 不存在。")
    stats = db.get_task_stats(project_name)
    if not db.delete_project(project_name):
        raise RuntimeError(f"项目 '{project_name}' 删除失败。")
    _append_event(f"删除项目：{project_name}", level="warning", project=project_name)
    return {
        "ok": True,
        "message": f"项目 '{project_name}' 已删除，工作目录保留。",
        "project": project_name,
        "path": project["path"],
        "deleted_tasks": stats["total"],
    }


def project_service_action(project: str, service: str, action: str) -> dict:
    db.init_db()
    project_name = (project or "").strip()
    if not project_name:
        raise RuntimeError("项目名称不能为空。")
    if not db.get_project(project_name):
        raise RuntimeError(f"项目 '{project_name}' 不存在。")
    service = (service or "").strip().lower()
    action = (action or "").strip().lower()
    if service not in {"tasks", "inspect"}:
        raise RuntimeError("服务只支持 tasks / inspect。")
    if action not in {"start", "stop", "status"}:
        raise RuntimeError("操作只支持 start / stop / status。")

    if service == "tasks":
        from codepilot.commands.daemon import daemon_service_status, request_daemon_service_start, stop_daemon_service
        if action == "start":
            result = request_daemon_service_start(project_name)
            msg = "任务执行服务已启动" if result.get("started") else "任务执行服务已在运行"
        elif action == "stop":
            result = stop_daemon_service(project_name)
            if result.get("stop_requested"):
                msg = "任务轮询已请求停止，当前任务完成后不会继续领取下一个任务"
            else:
                msg = "任务执行服务已停止" if result.get("stopped") else "任务执行服务未运行"
        else:
            result = daemon_service_status(project_name)
            msg = "任务执行服务状态已刷新"
    else:
        from codepilot.commands.inspect import inspect_service_status, request_inspect_service_start, stop_inspect_service
        if action == "start":
            result = request_inspect_service_start(project_name)
            msg = "巡检服务已启动" if result.get("started") else "巡检服务已在运行"
        elif action == "stop":
            result = stop_inspect_service(project_name)
            msg = "巡检服务已停止" if result.get("stopped") else "巡检服务未运行"
        else:
            result = inspect_service_status(project_name)
            msg = "巡检服务状态已刷新"

    level = "warning" if action == "stop" else "info"
    _append_event(f"{project_name}: {msg}", level=level, project=project_name)
    return {"ok": True, "message": msg, "project": project_name, "service": service, "action": action, "status": result}


def stop_task_action(task_id: int) -> dict:
    from click.testing import CliRunner
    from codepilot.commands.tasks import stop as stop_cmd

    result = CliRunner().invoke(stop_cmd, [str(task_id)])
    if result.exit_code != 0:
        raise RuntimeError(result.output.strip() or f"停止任务 #{task_id} 失败。")
    task = db.get_task(task_id)
    if task:
        _append_event(f"任务 #{task_id} 已收到停止请求。", level="warning", project=task["project"], task_id=task_id)
    return {"ok": True, "message": result.output.strip(), "task": _task_payload(task) if task else None}


def cancel_task_action(task_id: int, *, message: str = "") -> dict:
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")

    status = str(task.get("status") or "")
    if status == "in_progress":
        raise RuntimeError(f"任务 #{task_id} 正在执行中，不能取消；请在详情里使用停止。")
    if status == "done":
        raise RuntimeError(f"任务 #{task_id} 已完成，不能取消。")
    if status == "archived":
        raise RuntimeError(f"任务 #{task_id} 已归档，不能取消。")
    if status == "cancelled":
        raise RuntimeError(f"任务 #{task_id} 已取消，无需重复操作。")

    reason = (message or "手动取消").strip() or "手动取消"
    updated = clear_task_runtime(
        task_id,
        status="cancelled",
        completed_at=datetime.now().isoformat(timespec="seconds"),
        error_message=reason,
        stop_requested=0,
        stop_reason=None,
    )
    if not updated:
        raise RuntimeError(f"取消任务 #{task_id} 失败。")

    wt = task.get("worktree_path")
    project_path = task.get("project_path")
    try:
        if wt and wt != project_path:
            stop_worktree_leftovers(wt, wait_seconds=3)
    except Exception:  # noqa: BLE001
        # 清理 worktree 失败不应阻止任务取消
        pass

    _append_event(f"任务 #{task_id} 已取消。", level="warning", project=task["project"], task_id=task_id)
    return {"ok": True, "message": f"任务 #{task_id} 已取消。", "task": _task_payload(updated)}


def archive_task_action(task_id: int) -> dict:
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")

    status = str(task.get("status") or "")
    if status != "done":
        raise RuntimeError(f"任务 #{task_id} 当前状态为 {status}，只有已完成任务可以归档。")

    updated = db.update_task(task_id, status="archived")
    if not updated:
        raise RuntimeError(f"归档任务 #{task_id} 失败。")
    _append_event(f"任务 #{task_id} 已归档。", project=task["project"], task_id=task_id)
    return {"ok": True, "message": f"任务 #{task_id} 已归档。", "task": _task_payload(updated)}


def delete_task_action(task_id: int) -> dict:
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")

    status = str(task.get("status") or "")
    if status == "in_progress":
        raise RuntimeError(f"任务 #{task_id} 正在执行中，不能删除；请先停止。")
    if status not in {"backlog", "cancelled", "done", "archived"}:
        raise RuntimeError(f"任务 #{task_id} 当前状态为 {status}，不允许直接删除。")

    if not db.delete_task(task_id):
        raise RuntimeError(f"删除任务 #{task_id} 失败。")
    _append_event(f"任务 #{task_id} 已删除。", level="warning", project=task["project"], task_id=task_id)
    return {"ok": True, "message": f"任务 #{task_id} 已删除。", "deleted_task_id": task_id}


def batch_task_action(task_ids: list[int], action: str, *, message: str = "") -> dict:
    normalized_action = (action or "").strip().lower()
    handlers: dict[str, Callable[[int], dict]] = {
        "cancel": lambda tid: cancel_task_action(tid, message=message),
        "archive": archive_task_action,
        "delete": delete_task_action,
    }
    handler = handlers.get(normalized_action)
    if handler is None:
        raise RuntimeError("批量操作只支持 cancel / archive / delete。")

    if not isinstance(task_ids, list) or not task_ids:
        raise RuntimeError("task_ids 不能为空。")

    normalized_ids: list[int] = []
    seen: set[int] = set()
    for raw in task_ids:
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid <= 0 or tid in seen:
            continue
        seen.add(tid)
        normalized_ids.append(tid)

    if not normalized_ids:
        raise RuntimeError("task_ids 没有有效任务 ID。")

    succeeded: list[dict] = []
    failed: list[dict] = []
    for tid in normalized_ids:
        try:
            result = handler(tid)
            succeeded.append(
                {
                    "task_id": tid,
                    "message": str(result.get("message") or ""),
                    "task": result.get("task"),
                    "deleted_task_id": result.get("deleted_task_id"),
                }
            )
        except Exception as exc:
            failed.append({"task_id": tid, "error": str(exc)})

    success_count = len(succeeded)
    failed_count = len(failed)
    if failed_count:
        summary = f"批量{normalized_action}完成：成功 {success_count}，失败 {failed_count}。"
    else:
        summary = f"批量{normalized_action}完成：共 {success_count} 个任务。"

    return {
        "ok": failed_count == 0,
        "action": normalized_action,
        "total": len(normalized_ids),
        "success_count": success_count,
        "failed_count": failed_count,
        "succeeded": succeeded,
        "failed": failed,
        "message": summary,
    }


def get_task_template_schema_action(*, command_name: str = "codepilot") -> dict:
    return {"ok": True, "schema": task_template_schema(command_name=command_name)}


def _extract_batch_task_content(item: dict) -> str:
    raw = item.get("content")
    if raw in {"", None}:
        raw = item.get("body")
    if raw in {"", None}:
        raw = item.get("description")
    return str(raw or "")


def _normalize_batch_depends(raw_value) -> list[int] | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, (list, tuple)):
        raw_items = list(raw_value)
    elif isinstance(raw_value, str):
        raw_items = [part.strip() for part in raw_value.split(",")]
    else:
        raw_items = [raw_value]

    normalized: list[int] = []
    seen: set[int] = set()
    for item in raw_items:
        try:
            task_id = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        normalized.append(task_id)
    return normalized or None


def _batch_import_schema_constraints() -> tuple[list[str], set[str]]:
    schema = task_template_schema()
    validation = schema.get("validation") or {}
    return [
        str(item.get("name") or "").strip()
        for item in (schema.get("placeholders") or [])
        if isinstance(item, dict)
    ], {
        str(item).upper()
        for item in (validation.get("priority_values") or ["P0", "P1", "P2", "P3"])
    }


def _extract_batch_depends_value(raw: dict):
    if "depends" in raw:
        return raw.get("depends")
    if "depends_on" in raw:
        return raw.get("depends_on")
    return raw.get("dependsOn")


def _normalize_batch_import_item(
    raw: object,
    *,
    index: int,
    placeholder_names: list[str],
    allowed_priorities: set[str],
) -> tuple[dict | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"第 {index} 项必须是对象。"]

    title = " ".join(str(raw.get("title") or raw.get("name") or "").split())
    if not title:
        return None, [f"第 {index} 项缺少 title。"]

    content = _extract_batch_task_content(raw)
    if not content.strip():
        return None, [f"第 {index} 项《{title}》缺少 content。"]

    errors: list[str] = []
    missing = missing_task_template_sections(content)
    if missing:
        errors.append(f"第 {index} 项《{title}》缺少关键章节：{', '.join(missing)}。")

    leftovers = unreplaced_task_template_placeholders(
        content,
        placeholder_names=placeholder_names,
    )
    if leftovers:
        errors.append(f"第 {index} 项《{title}》仍包含未替换占位符：{', '.join(leftovers)}。")

    normalized_priority = str(raw.get("priority") or "P2").upper()
    if normalized_priority not in allowed_priorities:
        errors.append(f"第 {index} 项《{title}》优先级无效：{normalized_priority}。")

    return {
        "title": title,
        "content": content,
        "priority": normalized_priority,
        "agent": raw.get("agent"),
        "depends_on": _normalize_batch_depends(_extract_batch_depends_value(raw)),
    }, errors


def _normalize_batch_import_items(items: list[dict]) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise RuntimeError("items 必须是非空 JSON 数组。")

    placeholder_names, allowed_priorities = _batch_import_schema_constraints()
    normalized_items: list[dict] = []
    validation_errors: list[str] = []
    for index, raw in enumerate(items, 1):
        normalized_item, item_errors = _normalize_batch_import_item(
            raw,
            index=index,
            placeholder_names=placeholder_names,
            allowed_priorities=allowed_priorities,
        )
        validation_errors.extend(item_errors)
        if normalized_item is not None:
            normalized_items.append(normalized_item)

    if validation_errors:
        raise RuntimeError(" ".join(validation_errors))
    return normalized_items


def _create_batch_import_tasks(project: str, normalized_items: list[dict]) -> list[dict]:
    created: list[dict] = []
    for item in normalized_items:
        task = create_task_action(
            project,
            item["title"],
            content=item["content"],
            priority=item["priority"],
            agent=item["agent"] or None,
            mode="full",
        )["task"]
        if item["depends_on"]:
            updated = db.update_task(task["id"], depends_on=item["depends_on"])
            task = _task_payload(updated or db.get_task(task["id"]))
        created.append(task)
    return created


def import_tasks_action(project: str, items: list[dict]) -> dict:
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    normalized_items = _normalize_batch_import_items(items)

    created = _create_batch_import_tasks(project, normalized_items)

    _append_event(
        f"批量导入 {len(created)} 个任务。",
        project=project,
        task_id=created[0]["id"] if created else None,
    )
    return {
        "ok": True,
        "count": len(created),
        "tasks": created,
        "message": f"批量导入完成：共 {len(created)} 个任务。",
    }


def create_task_action(
    project: str,
    title: str,
    *,
    content: str = "",
    priority: str = "P2",
    agent: str | None = None,
    max_retries: int = 3,
    mode: str = "full",
) -> dict:
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    normalized_title = " ".join((title or "").split())
    if not normalized_title:
        raise RuntimeError("任务标题不能为空。")
    normalized_priority = (priority or "P2").upper()
    if normalized_priority not in {"P0", "P1", "P2", "P3"}:
        raise RuntimeError("优先级只支持 P0 / P1 / P2 / P3。")

    normalized_mode = (mode or "full").strip().lower()
    if normalized_mode == "requirement":
        raise RuntimeError(
            "mode=requirement 应该投递到 /api/requirements，让规划器拆分需求；"
            "/api/tasks 仅用于 mode=full（完整任务）或 mode=ai_complete（仅标题，AI 补全）。"
        )
    if normalized_mode not in {"full", "ai_complete"}:
        raise RuntimeError(
            f"未知的添加模式 '{mode}'，只支持 full / ai_complete / requirement。"
        )

    resolved_agent = (agent or project_info.get("default_mode") or "dual").lower()
    if resolved_agent == "auto":
        resolved_agent = project_info.get("default_mode") or "dual"

    from codepilot.ai_support.service import resolve_agent_with_fallback, generate_task_content
    default_mode = project_info.get("default_mode") or "dual"
    resolved_agent, fallback_reason = resolve_agent_with_fallback(
        resolved_agent,
        project_path=project_info["path"],
        default_mode=default_mode,
    )

    if normalized_mode == "ai_complete":
        try:
            content = generate_task_content(
                normalized_title,
                project_path=project_info["path"],
                agent=resolved_agent,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"AI 生成任务内容失败: {exc}") from exc

    final_content = (content or "").strip()
    if not final_content:
        raise RuntimeError(
            "任务内容为空：mode=full 必须提供完整 content；mode=ai_complete 由 AI 生成，"
            "若仍为空说明 agent 未给出内容。"
        )
    missing = missing_task_template_sections(final_content)
    if missing:
        source = "AI 生成的" if normalized_mode == "ai_complete" else "提交的"
        raise RuntimeError(
            f"{source} content 缺少模板必需章节: {', '.join(missing)}。"
            " 字段规范见 /api/tasks/template 或 `codepilot ai template --format json`。"
        )

    task = db.create_task(
        project=project,
        title=normalized_title,
        content=final_content,
        agent=resolved_agent,
        priority=normalized_priority,
        project_path=project_info["path"],
        max_retries=max(0, int(max_retries or 0)),
        fallback_reason=fallback_reason,
    )
    msg = f"任务 #{task['id']} 已创建。"
    if fallback_reason:
        msg += f" （{fallback_reason}）"
    _append_event(f"已新建任务 #{task['id']}：{normalized_title}", project=project, task_id=task["id"])
    return {"ok": True, "message": msg, "task": _task_payload(task), "mode": normalized_mode}
