"""Local Web UI for browsing and operating CodePilot projects and tasks."""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from codepilot import db
from codepilot.commands.auto import run_requirement_workflow
from codepilot.config import load_project_config
from codepilot.runtime import runtime_summary

_GOAL_MAX_BYTES = 4096


STATUS_ORDER = {"in_progress": 0, "backlog": 1, "failed": 2, "cancelled": 3, "done": 4}
_UI_LOCK = threading.Lock()
_UI_JOB_SEQ = 0
_UI_JOBS: dict[int, dict] = {}
_UI_EVENTS: list[dict] = []
_MAX_EVENTS = 40
_MAX_JOB_LOG_LINES = 50


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tail_text(text: str, *, max_lines: int = 160, max_chars: int = 20000) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
    return text[-max_chars:] if len(text) > max_chars else text


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    target = Path(path)
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


def _parse_depends(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [int(item) for item in items if str(item).strip()]


def _append_event(message: str, *, level: str = "info", project: str | None = None, task_id: int | None = None) -> None:
    entry = {
        "time": _now_iso(),
        "level": level,
        "project": project or "",
        "task_id": task_id,
        "message": message,
    }
    with _UI_LOCK:
        _UI_EVENTS.append(entry)
        del _UI_EVENTS[:-_MAX_EVENTS]


def _next_job_id() -> int:
    global _UI_JOB_SEQ
    with _UI_LOCK:
        _UI_JOB_SEQ += 1
        return _UI_JOB_SEQ


def _update_job(job_id: int, **fields) -> dict:
    with _UI_LOCK:
        job = _UI_JOBS[job_id]
        job.update(fields)
        return dict(job)


def list_ui_jobs(project: str | None = None) -> list[dict]:
    with _UI_LOCK:
        items = [dict(job) for job in _UI_JOBS.values()]
    if project:
        items = [job for job in items if job.get("project") == project]
    items.sort(key=lambda item: (item.get("updated_at") or item.get("created_at") or "", item["id"]), reverse=True)
    return items[:12]


def list_ui_events(project: str | None = None) -> list[dict]:
    with _UI_LOCK:
        items = list(_UI_EVENTS)
    if project:
        items = [event for event in items if not event.get("project") or event.get("project") == project]
    return list(reversed(items[-12:]))


def _compose_log_text(task: dict) -> str:
    live = _read_text(task.get("current_log_path"))
    if live:
        return _tail_text(live)

    logs = db.list_task_logs(task["id"])
    if logs:
        blocks = []
        for entry in logs:
            header = f"[{entry.get('phase') or '-'}] agent={entry.get('agent') or '-'} exit={entry.get('exit_code') if entry.get('exit_code') is not None else '-'}"
            blocks.append(header)
            if entry.get("output"):
                blocks.append(entry["output"].strip())
        return _tail_text("\n\n".join(blocks))

    return _tail_text(task.get("last_output") or task.get("error_message") or task.get("delivery_record") or "")


def _task_payload(task: dict) -> dict:
    status = task["status"]
    return {
        "id": task["id"],
        "project": task["project"],
        "title": task["title"],
        "status": status,
        "priority": task["priority"],
        "agent": task["agent"],
        "source": task.get("source") or "user",
        "phase": task.get("run_phase") or "",
        "runtime": runtime_summary(task) if status == "in_progress" else "",
        "latest": task.get("last_output") or task.get("error_message") or task.get("delivery_record") or "",
        "error_message": task.get("error_message") or "",
        "delivery_record": task.get("delivery_record") or "",
        "created_at": task.get("created_at") or "",
        "started_at": task.get("started_at") or "",
        "completed_at": task.get("completed_at") or "",
        "retry_count": int(task.get("retry_count") or 0),
        "max_retries": int(task.get("max_retries") or 0),
        "actions": {
            "retry": status in {"failed", "cancelled", "backlog"},
            "stop": status == "in_progress",
            "promote": status in {"backlog", "failed", "cancelled"},
        },
    }


def _sorted_tasks(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda item: (STATUS_ORDER.get(item["status"], 9), item["priority"], item["id"]))


def project_summary(project: dict) -> dict:
    stats = db.get_task_stats(project["name"])
    tasks = _sorted_tasks(db.list_tasks(project=project["name"]))
    live = next((task for task in tasks if task["status"] == "in_progress"), None)
    return {
        "name": project["name"],
        "path": project["path"],
        "stats": stats,
        "active_summary": runtime_summary(live) if live else "",
    }


def dashboard_payload(selected_project: str | None = None) -> dict:
    db.init_db()
    projects = [project_summary(project) for project in db.list_projects()]
    resolved = selected_project or (projects[0]["name"] if projects else None)
    tasks = _sorted_tasks(db.list_tasks(project=resolved)) if resolved else []
    return {
        "projects": projects,
        "selected_project": resolved,
        "tasks": [_task_payload(task) for task in tasks],
        "jobs": list_ui_jobs(resolved),
        "events": list_ui_events(resolved),
    }


def task_detail_payload(task_id: int) -> dict:
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    payload = _task_payload(task)
    payload.update(
        {
            "content": task.get("content") or "",
            "depends_on": _parse_depends(task.get("depends_on")),
            "project_path": task.get("project_path") or "",
            "current_log_path": task.get("current_log_path") or "",
            "log_text": _compose_log_text(task),
            "logs": [
                {
                    "phase": entry.get("phase") or "",
                    "agent": entry.get("agent") or "",
                    "exit_code": entry.get("exit_code"),
                    "output_excerpt": _tail_text(entry.get("output") or "", max_lines=40, max_chars=5000),
                }
                for entry in db.list_task_logs(task_id)
            ],
        }
    )
    return payload

def retry_task_action(task_id: int) -> dict:
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    if task["status"] == "in_progress":
        raise RuntimeError(f"任务 #{task_id} 正在运行中，请先停止再重试。")
    if task["status"] == "done":
        raise RuntimeError(f"任务 #{task_id} 已完成，不能直接重试。")
    updated = db.reset_task_for_retry(task_id)
    _append_event(f"任务 #{task_id} 已重新放回 backlog。", project=task["project"], task_id=task_id)
    return {"ok": True, "message": f"任务 #{task_id} 已重新放回 backlog。", "task": _task_payload(updated)}


def promote_task_action(task_id: int) -> dict:
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    if task["status"] == "done":
        raise RuntimeError(f"任务 #{task_id} 已完成，不能插队。")
    if task["status"] == "in_progress":
        raise RuntimeError(f"任务 #{task_id} 正在执行中，无需插队。")
    if task["status"] in {"failed", "cancelled"}:
        task = db.reset_task_for_retry(task_id, reset_retry_count=False)
    updated = db.update_task(task_id, priority="P0", status="backlog")
    _append_event(f"任务 #{task_id} 已插队到 P0。", project=task["project"], task_id=task_id)
    return {"ok": True, "message": f"任务 #{task_id} 已提升到 P0 并回到 backlog。", "task": _task_payload(updated)}


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


def create_task_action(
    project: str,
    title: str,
    *,
    content: str = "",
    priority: str = "P2",
    agent: str | None = None,
    max_retries: int = 3,
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
    resolved_agent = (agent or project_info.get("default_mode") or "codex").lower()
    if resolved_agent == "auto":
        resolved_agent = project_info.get("default_mode") or "codex"

    # Fallback when the requested agent is unavailable (e.g. missing API key).
    from codepilot.ai import resolve_agent_with_fallback
    default_mode = project_info.get("default_mode") or "codex"
    resolved_agent, fallback_reason = resolve_agent_with_fallback(
        resolved_agent,
        project_path=project_info["path"],
        default_mode=default_mode,
    )

    task = db.create_task(
        project=project,
        title=normalized_title,
        content=content,
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
    return {"ok": True, "message": msg, "task": _task_payload(task)}


def _job_result_summary(result: dict, execute: bool) -> str:
    task_count = len(result.get("tasks") or [])
    parts = [f"创建 {task_count} 个任务"]
    if result.get("summary"):
        parts.append(result["summary"])
    run_stats = result.get("run") or {}
    if execute and run_stats:
        parts.append(f"执行结果: done={run_stats.get('done', 0)} failed={run_stats.get('failed', 0)} requeued={run_stats.get('requeued', 0)}")
    return " | ".join(parts)


def submit_requirement_action(
    project: str,
    title: str,
    *,
    execute: bool = True,
    planner: str = "codex",
    agent: str | None = None,
    priority: str = "P2",
    max_tasks: int = 5,
    executor: str = "auto",
    auto_commit: bool = False,
    max_retries: int = 3,
    run_async: bool = True,
) -> dict:
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    normalized_title = " ".join((title or "").split())
    if not normalized_title:
        raise RuntimeError("需求文本不能为空。")
    job_id = _next_job_id()
    with _UI_LOCK:
        _UI_JOBS[job_id] = {
            "id": job_id,
            "type": "requirement",
            "project": project,
            "title": normalized_title,
            "planner": planner,
            "agent": (agent or "").lower(),
            "status": "queued",
            "phase": "queued",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "finished_at": "",
            "summary": "",
            "error": "",
            "task_ids": [],
            "log": [],
        }
    _append_event(f"收到需求：{normalized_title}", project=project)

    def _append_job_log(line: str) -> None:
        with _UI_LOCK:
            job = _UI_JOBS.get(job_id)
            if job:
                job["log"].append(line)
                if len(job["log"]) > _MAX_JOB_LOG_LINES:
                    del job["log"][:-_MAX_JOB_LOG_LINES]
                job["updated_at"] = _now_iso()

    def worker() -> None:
        import codepilot.ai as _ai_module

        _update_job(job_id, status="running", phase="planning", updated_at=_now_iso())
        _append_job_log(f"开始规划：{normalized_title}")
        _append_job_log(f"使用规划器：{planner}")

        # Hook planner progress callback
        prev_callback = _ai_module._planner_progress_callback
        _ai_module._planner_progress_callback = _append_job_log
        try:
            result = run_requirement_workflow(
                project_info=db.get_project(project) or project_info,
                title=normalized_title,
                planner=planner,
                task_agent=agent or None,
                priority=priority,
                max_tasks=max_tasks,
                execute=execute,
                executor=executor,
                auto_commit=auto_commit,
                max_retries=max_retries,
                json_mode=False,
            )
            task_ids = [item["id"] for item in (result.get("tasks") or [])]
            run_stats = result.get("run") or {}
            status = "attention" if execute and run_stats.get("failed") else "succeeded"
            _append_job_log(f"规划完成，创建 {len(task_ids)} 个任务")
            if execute and run_stats:
                _append_job_log(f"执行结果: done={run_stats.get('done', 0)} failed={run_stats.get('failed', 0)}")
            _update_job(
                job_id,
                status=status,
                phase="done" if status == "succeeded" else "attention",
                updated_at=_now_iso(),
                finished_at=_now_iso(),
                summary=_job_result_summary(result, execute),
                task_ids=task_ids,
                error="",
            )
            _append_event(
                f"需求处理完成：{normalized_title}",
                level="warning" if status == "attention" else "info",
                project=project,
                task_id=task_ids[0] if task_ids else None,
            )
        except Exception as exc:
            _append_job_log(f"失败：{exc}")
            _update_job(
                job_id,
                status="failed",
                phase="failed",
                updated_at=_now_iso(),
                finished_at=_now_iso(),
                error=str(exc),
            )
            _append_event(f"需求执行失败：{normalized_title} | {exc}", level="error", project=project)
        finally:
            _ai_module._planner_progress_callback = prev_callback

    if run_async:
        threading.Thread(target=worker, name=f"codepilot-ui-job-{job_id}", daemon=True).start()
    else:
        worker()

    return {"ok": True, "message": f"需求已提交，后台任务 #{job_id} 已启动。", "job": dict(_UI_JOBS[job_id])}


def submit_goal_action(project: str, text: str, *, category: str = "auto") -> dict:
    """POST /api/goal — classify intent and route accordingly.

    *category* can be ``auto``, ``question``, ``requirement``, or ``command``.
    """
    from codepilot.ai import answer_question_via_api, classify_intent

    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("输入不能为空。")
    if len(text.encode("utf-8")) > _GOAL_MAX_BYTES:
        raise RuntimeError(f"输入超过 {_GOAL_MAX_BYTES // 1024}KB 限制。")

    category = (category or "auto").lower()
    valid_categories = {"auto", "question", "requirement", "command"}
    if category not in valid_categories:
        category = "auto"

    # --- resolve intent ---
    if category == "auto":
        cfg = load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))
        classifier_cfg = getattr(cfg, "classifier", None)
        api_key = None
        if classifier_cfg and classifier_cfg.enabled:
            if classifier_cfg.provider:
                api_key = cfg.get_provider_api_key(classifier_cfg.provider)
            try:
                result = classify_intent(
                    text,
                    project_path=project_info["path"],
                    classifier_provider=classifier_cfg.provider,
                    classifier_model=classifier_cfg.model,
                    timeout=classifier_cfg.timeout,
                    api_key=api_key,
                )
                intent = result["intent"]
            except Exception:
                intent = "requirement"
        else:
            intent = "requirement"
    else:
        intent = category

    # --- route ---
    if intent == "command":
        _append_event(f"收到命令类输入（已提示用户使用 CLI）：{text[:60]}", project=project)
        return {
            "ok": True,
            "intent": "command",
            "message": (
                "这看起来是在调用 codepilot 自身命令，请在终端直接执行：\n"
                "  状态总览:  codepilot status -p <项目> -v\n"
                "  任务日志:  codepilot logs <task_id>\n"
                "  重试任务:  codepilot retry <task_id>\n"
                "  停止任务:  codepilot stop <task_id>\n"
                "  触发巡检:  codepilot inspect -p <项目>"
            ),
        }

    if intent == "question":
        cfg = load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))
        classifier_cfg = getattr(cfg, "classifier", None)
        provider_key = classifier_cfg.provider if classifier_cfg else ""
        api_key = cfg.get_provider_api_key(provider_key) if provider_key else None
        try:
            answer = answer_question_via_api(
                provider_key=provider_key,
                question=text,
                project_path=project_info["path"],
                model_override=classifier_cfg.model if classifier_cfg else "",
                api_key=api_key,
            )
        except Exception as exc:
            answer = f"回答失败：{exc}"
        _append_event(f"回答问题：{text[:60]}", project=project)
        return {"ok": True, "intent": "question", "message": answer or "未获得回答"}

    # requirement or task — delegate to the existing requirement flow
    max_tasks = 1 if intent == "task" else 5
    result = submit_requirement_action(
        project,
        text,
        execute=True,
        max_tasks=max_tasks,
        run_async=True,
    )
    result["intent"] = intent
    return result


# ── Session actions ────────────────────────────────────────────────────────


def list_sessions_action(project: str = "") -> dict:
    db.init_db()
    sessions = db.list_sessions(project=project or None, status="active")
    return {
        "ok": True,
        "sessions": [
            {
                "id": s["id"],
                "project": s["project"],
                "title": s["title"],
                "status": s["status"],
                "created_at": s["created_at"],
                "updated_at": s["updated_at"],
                "message_count": len(db.list_session_messages(s["id"])),
            }
            for s in sessions
        ],
    }


def create_session_action(project: str, title: str = "") -> dict:
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    session = db.create_session(project, title=title or "新会话")
    _append_event(f"新建会话 #{session['id']}：{session['title']}", project=project)
    return {"ok": True, "session": session}


def get_session_action(session_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    messages = db.list_session_messages(session_id)
    parsed_messages = []
    for msg in messages:
        parsed = dict(msg)
        if parsed.get("task_ids"):
            try:
                parsed["task_ids"] = json.loads(parsed["task_ids"])
            except (json.JSONDecodeError, TypeError):
                parsed["task_ids"] = []
        else:
            parsed["task_ids"] = []
        parsed_messages.append(parsed)
    return {"ok": True, "session": session, "messages": parsed_messages}


def send_session_message_action(session_id: int, text: str, *, category: str = "auto") -> dict:
    """Send a message in a session — classify intent, route, and record both user and assistant messages."""
    from codepilot.ai import answer_question_via_api, classify_intent

    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    project = session["project"]
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("输入不能为空。")

    # Auto-update session title from first message
    existing_messages = db.list_session_messages(session_id)
    if not existing_messages:
        short_title = text[:40] + ("…" if len(text) > 40 else "")
        db.update_session(session_id, title=short_title)

    # Record user message
    db.create_session_message(session_id, "user", text)

    # Classify intent
    category = (category or "auto").lower()
    if category == "auto":
        cfg = load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))
        classifier_cfg = getattr(cfg, "classifier", None)
        api_key = None
        if classifier_cfg and classifier_cfg.enabled:
            if classifier_cfg.provider:
                api_key = cfg.get_provider_api_key(classifier_cfg.provider)
            try:
                result = classify_intent(
                    text,
                    project_path=project_info["path"],
                    classifier_provider=classifier_cfg.provider,
                    classifier_model=classifier_cfg.model,
                    timeout=classifier_cfg.timeout,
                    api_key=api_key,
                )
                intent = result["intent"]
            except Exception:
                intent = "requirement"
        else:
            intent = "requirement"
    else:
        intent = category

    # Route by intent
    if intent == "command":
        reply = (
            "这看起来是在调用 codepilot 自身命令，请在终端直接执行：\n"
            "  状态总览:  codepilot status -p <项目> -v\n"
            "  任务日志:  codepilot logs <task_id>\n"
            "  重试任务:  codepilot retry <task_id>\n"
            "  停止任务:  codepilot stop <task_id>"
        )
        db.create_session_message(session_id, "assistant", reply, intent="command")
        return {"ok": True, "intent": "command", "message": reply, "task_ids": []}

    if intent == "question":
        cfg = load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))
        classifier_cfg = getattr(cfg, "classifier", None)
        provider_key = classifier_cfg.provider if classifier_cfg else ""
        api_key = cfg.get_provider_api_key(provider_key) if provider_key else None
        try:
            answer = answer_question_via_api(
                provider_key=provider_key,
                question=text,
                project_path=project_info["path"],
                model_override=classifier_cfg.model if classifier_cfg else "",
                api_key=api_key,
            )
        except Exception as exc:
            answer = f"回答失败：{exc}"
        reply = answer or "未获得回答"
        db.create_session_message(session_id, "assistant", reply, intent="question")
        return {"ok": True, "intent": "question", "message": reply, "task_ids": []}

    # requirement / task — create tasks
    max_tasks = 1 if intent == "task" else 5
    result = submit_requirement_action(project, text, execute=True, max_tasks=max_tasks, run_async=True)
    task_ids = []
    job = result.get("job")
    if job:
        task_ids = job.get("task_ids") or []
    reply = result.get("message") or "需求已提交"
    db.create_session_message(session_id, "assistant", reply, intent=intent, task_ids=task_ids)
    return {"ok": True, "intent": intent, "message": reply, "task_ids": task_ids}


def delete_session_action(session_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    db.delete_session(session_id)
    _append_event(f"删除会话 #{session_id}", project=session["project"])
    return {"ok": True, "message": f"会话 #{session_id} 已删除。"}


HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CodePilot 控制台</title>
<style>
:root{--bg:#f6f0e7;--panel:#fffdf8;--line:#e1d9cf;--text:#1f2933;--muted:#64707d;--brand:#0f6c9b;--brand-bg:#eaf6fb;--ok:#1b8b57;--ok-bg:#e9f8f1;--warn:#b87900;--warn-bg:#fff4de;--danger:#c43f52;--danger-bg:#fcebef;--shadow:0 18px 36px rgba(31,41,51,.10);font-family:"Segoe UI Variable","PingFang SC","Microsoft YaHei UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#f7f2ea,#edf2f6);color:var(--text)}
.shell{display:grid;grid-template-columns:300px 1fr;gap:20px;padding:20px;min-height:100vh}.side,.main{background:rgba(255,255,255,.9);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow);backdrop-filter:blur(14px)}
.side{padding:18px;display:flex;flex-direction:column;gap:16px;overflow-y:auto;max-height:100vh}.main{padding:20px;display:flex;flex-direction:column;gap:16px}.title{margin:0;font-size:28px}.muted{color:var(--muted);line-height:1.5}
.toolbar,.split{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}.btn{border:0;border-radius:12px;padding:10px 14px;font:inherit;font-weight:700;cursor:pointer;background:var(--brand-bg);color:var(--brand);transition:opacity .15s}.btn:disabled{opacity:.5;cursor:wait}
.btn.secondary{background:#f1f3f5;color:var(--text)}.btn.warn{background:var(--warn-bg);color:var(--warn)}.btn.danger{background:var(--danger-bg);color:var(--danger)}.btn.ok{background:var(--ok-bg);color:var(--ok)}
.btn.sm{padding:6px 10px;font-size:12px;border-radius:8px}
.card,.task,.job,.event,.detail{border:1px solid var(--line);border-radius:18px;background:var(--panel);padding:14px}.card.active,.task.sel{border-color:#9dccdf;background:#f4fbff}.list{display:flex;flex-direction:column;gap:10px}.scroll{max-height:calc(100vh - 260px);overflow:auto;padding-right:4px}
.tag{display:inline-flex;align-items:center;justify-content:center;padding:5px 10px;border-radius:999px;font-size:12px;font-weight:800}.status-backlog{background:#f1f3f5}.status-in_progress,.status-running{background:var(--brand-bg);color:var(--brand)}.status-failed,.status-cancelled,.status-error{background:var(--danger-bg);color:var(--danger)}.status-done,.status-succeeded{background:var(--ok-bg);color:var(--ok)}.status-warning,.status-attention{background:var(--warn-bg);color:var(--warn)}.status-queued{background:#f1f3f5}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.metric{border:1px solid var(--line);border-radius:16px;padding:14px;background:var(--panel)}.metric b{display:block;font-size:28px;margin-top:8px}
.hero,.workspace{display:grid;grid-template-columns:1fr 1fr;gap:16px}.workspace{grid-template-columns:1.1fr .9fr}.field{display:flex;flex-direction:column;gap:6px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.grid .wide{grid-column:1/-1}
input,select,textarea{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:12px;background:#fff;font:inherit;color:var(--text)}textarea{min-height:100px;resize:vertical;line-height:1.55}
.sections{display:flex;flex-direction:column;gap:16px}.task{cursor:pointer}.task:hover{border-color:#9dccdf}.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;color:var(--muted);font-size:12px}.body{margin-top:10px;white-space:pre-wrap;word-break:break-word;line-height:1.55}
.banner{display:none;padding:12px 14px;border-radius:14px;font-weight:700;white-space:pre-wrap}.banner.show{display:block}.banner.info{background:var(--brand-bg);color:var(--brand)}.banner.success{background:var(--ok-bg);color:var(--ok)}.banner.error{background:var(--danger-bg);color:var(--danger)}
.empty{padding:18px;border:1px dashed var(--line);border-radius:14px;text-align:center;color:var(--muted)}pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:"Cascadia Code","Consolas",monospace;font-size:12px;line-height:1.6;max-height:300px;overflow:auto}
.tab-bar{display:flex;gap:4px;border-bottom:2px solid var(--line);padding-bottom:0}.tab-bar .tab{padding:10px 16px;cursor:pointer;border-radius:12px 12px 0 0;font-weight:700;color:var(--muted);border:1px solid transparent;border-bottom:none;margin-bottom:-2px}.tab-bar .tab.active{color:var(--brand);background:var(--panel);border-color:var(--line)}
.view{display:none}.view.active{display:flex;flex-direction:column;gap:16px}
.chat-messages{display:flex;flex-direction:column;gap:10px;min-height:200px;max-height:55vh;overflow-y:auto;padding:10px 4px}
.chat-msg{padding:10px 14px;border-radius:14px;max-width:85%;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.chat-msg.user{align-self:flex-end;background:var(--brand-bg);color:var(--brand)}.chat-msg.assistant{align-self:flex-start;background:var(--panel);border:1px solid var(--line)}
.chat-msg .msg-meta{font-size:11px;color:var(--muted);margin-top:4px}
.session-item{cursor:pointer;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.session-item:hover{border-color:#9dccdf}.session-item.active{border-color:#9dccdf;background:#f4fbff}
.session-item .split{align-items:center}
.session-project-label{font-size:11px;color:var(--muted);background:#f1f3f5;padding:2px 8px;border-radius:6px;margin-top:2px;display:inline-block}
.job-active{border-color:var(--brand);background:var(--brand-bg);animation:jobPulse 2s ease-in-out infinite}
@keyframes jobPulse{0%,100%{opacity:1}50%{opacity:.85}}
.job-spinner{display:inline-block;width:12px;height:12px;border:2px solid var(--brand);border-top-color:transparent;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:4px}
@keyframes spin{to{transform:rotate(360deg)}}
.job-log{margin:6px 0 0;padding:6px 8px;background:#f8f6f3;border:1px solid var(--line);border-radius:8px;font-size:11px;max-height:100px;overflow-y:auto;color:var(--muted)}
@media(max-width:1180px){.shell,.hero,.workspace{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.scroll{max-height:none}.side{max-height:none}}@media(max-width:720px){.grid,.metrics{grid-template-columns:1fr}}
</style></head><body>
<div class="shell">
<aside class="side">
  <div><h1 class="title">CodePilot</h1><div class="muted">直接提需求，盯住任务、日志和失败原因。</div></div>
  <section><div class="muted">项目</div><div id="projects" class="list"></div></section>
  <section>
    <div class="split" style="align-items:center"><div class="muted">全部会话</div><button id="newSessionBtn" class="btn sm ok">+ 新建</button></div>
    <div id="sessions" class="list" style="margin-top:8px;max-height:30vh;overflow-y:auto"></div>
  </section>
  <section><div class="muted">最近需求</div><div id="jobs" class="list"></div></section>
  <section><div class="muted">最近事件</div><div id="events" class="list"></div></section>
</aside>
<main class="main">
  <div class="toolbar"><div><h2 id="projectTitle" style="margin:0">项目总览</h2><div id="projectPath" class="muted">正在读取数据…</div></div><div class="split"><button id="refreshBtn" class="btn secondary">立即刷新</button><button id="toggleBtn" class="btn">自动刷新：开</button></div></div>
  <!-- Banner OUTSIDE views so it is always visible -->
  <div id="banner" class="banner"></div>
  <div id="goalAnswer" class="banner"></div>
  <div class="tab-bar">
    <div class="tab active" data-view="dashboard">控制台</div>
    <div class="tab" data-view="chat">会话</div>
  </div>
  <!-- Dashboard view -->
  <div id="dashboardView" class="view active">
  <div id="goalBar" class="detail" style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
    <div class="field" style="flex:1;min-width:220px"><label>快速输入</label><input id="goalText" type="text" placeholder="输入问题、需求或命令…" maxlength="4096"></div>
    <div class="field" style="width:120px"><label>类型</label><select id="goalCategory"><option value="auto" selected>自动</option><option value="question">问题</option><option value="requirement">需求</option><option value="command">命令</option></select></div>
    <button id="goalSubmit" class="btn ok" style="height:42px;white-space:nowrap">提交</button>
  </div>
  <div class="hero">
    <section class="detail">
      <div class="split"><div><h3 style="margin:0">发起工作</h3><div class="muted">可直接提交自然语言需求，也可直接建任务。</div></div></div>
      <form id="composer">
        <div class="grid">
          <div class="field"><label>模式</label><select id="mode"><option value="requirement">自然语言需求</option><option value="task">直接建任务</option></select></div>
          <div class="field"><label>优先级</label><select id="priority"><option>P0</option><option>P1</option><option selected>P2</option><option>P3</option></select></div>
          <div class="field wide"><label>标题</label><textarea id="titleInput" placeholder="例如：把失败任务的原因直接显示在 UI 里，并且可以一键重试"></textarea></div>
          <div class="field wide"><label>补充说明</label><textarea id="contentInput" placeholder="可选：范围、约束、验收标准。直接建任务时会写入任务内容。"></textarea></div>
          <div class="field"><label>任务智能体</label><select id="agent"><option value="auto" selected>跟随项目默认</option><option value="codex">codex</option><option value="claude">claude</option></select></div>
          <div class="field" id="plannerWrap"><label>规划智能体</label><select id="planner"><option value="codex" selected>codex</option><option value="claude">claude</option></select></div>
        </div>
        <div class="split" style="margin-top:12px"><label class="muted"><input id="executeNow" type="checkbox" checked> 提交后立即执行</label><button id="composerSubmit" class="btn ok" type="submit">提交到当前项目</button></div>
      </form>
    </section>
    <section class="detail"><h3 style="margin:0 0 12px">项目状态</h3><div id="metrics" class="metrics"></div></section>
  </div>
  <div class="workspace">
    <section class="sections">
      <div class="detail"><h3 style="margin:0 0 12px">进行中</h3><div id="running" class="list"></div></div>
      <div class="detail"><h3 style="margin:0 0 12px">待办 / 可重试</h3><div id="backlog" class="list"></div></div>
      <div class="detail"><h3 style="margin:0 0 12px">失败 / 已取消</h3><div id="failed" class="list"></div></div>
      <div class="detail"><h3 style="margin:0 0 12px">最近完成</h3><div id="done" class="list"></div></div>
    </section>
    <section class="detail"><h3 style="margin:0 0 12px">任务详情</h3><div id="taskDetail" class="list"><div class="empty">先选择一个任务。</div></div></section>
  </div>
  </div>
  <!-- Chat/Session view -->
  <div id="chatView" class="view">
    <div id="chatHeader" class="detail" style="padding:10px 14px">
      <div class="split" style="align-items:center">
        <div><strong id="chatSessionTitle">选择或新建一个会话</strong><div id="chatSessionMeta" class="muted" style="font-size:12px"></div></div>
        <div class="split" style="gap:6px"><button id="deleteSessionBtn" class="btn sm danger" style="display:none">删除会话</button></div>
      </div>
    </div>
    <div id="chatMessages" class="chat-messages"><div class="empty">从左侧选择一个会话，或点击「+ 新建」开始。</div></div>
    <div id="chatInputBar" class="detail" style="display:none;gap:10px;align-items:flex-end;flex-wrap:wrap;padding:10px 14px">
      <div class="field" style="flex:1;min-width:200px"><input id="chatInput" type="text" placeholder="在此会话中输入问题或需求…" maxlength="4096"></div>
      <div class="field" style="width:100px"><select id="chatCategory"><option value="auto" selected>自动</option><option value="question">问题</option><option value="requirement">需求</option></select></div>
      <button id="chatSend" class="btn ok" style="height:42px;white-space:nowrap">发送</button>
    </div>
  </div>
</main></div>
<script>
"use strict";
const S={project:null,taskId:null,auto:true,timer:null,view:'dashboard',sessionId:null,busy:false};
const esc=(v)=>String(v||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const fmtTime=(v)=>v?v.replace('T',' ').slice(0,19):'-';
const stsCls=(s)=>'status-'+String(s||'').replace(/ /g,'_');
function $(id){return document.getElementById(id)}
async function getj(url){const r=await fetch(url);const d=await r.json();if(!r.ok)throw new Error(d.error||r.statusText||'请求失败');return d}
async function postj(url,p){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p||{})});const d=await r.json();if(!r.ok)throw new Error(d.error||r.statusText||'请求失败');return d}
async function delj(url){const r=await fetch(url,{method:'DELETE'});const d=await r.json();if(!r.ok)throw new Error(d.error||r.statusText||'请求失败');return d}
function flash(msg,type){const b=$('banner');if(!msg){b.className='banner';b.textContent='';return}b.className='banner show '+(type||'info');b.textContent=msg;if(type==='success')setTimeout(()=>{if(b.textContent===msg)flash('')},4000)}
function flashAnswer(msg){const b=$('goalAnswer');if(!msg){b.className='banner';b.textContent='';return}b.className='banner show info';b.textContent=msg}
function setLoading(btn,loading,label){if(!btn)return;btn.disabled=loading;if(loading){btn._origText=btn._origText||btn.textContent;btn.textContent='处理中...'}else{btn.textContent=btn._origText||label||'提交'}}
function metric(label,val){return '<div class="metric"><div class="muted">'+esc(label)+'</div><b>'+esc(val)+'</b></div>'}
function projectCard(p){var s=p.stats||{};return '<div class="card '+(p.name===S.project?'active':'')+'" data-project="'+esc(p.name)+'"><div class="split"><strong>'+esc(p.name)+'</strong><span class="tag '+(s.in_progress?'status-in_progress':'status-backlog')+'">'+((s.total||0))+'</span></div><div class="meta"><span class="tag status-in_progress">'+('进行中 '+(s.in_progress||0))+'</span><span class="tag status-backlog">'+('待办 '+(s.backlog||0))+'</span><span class="tag status-failed">'+('失败 '+((s.failed||0)+(s.cancelled||0)))+'</span><span class="tag status-done">'+('完成 '+(s.done||0))+'</span></div><div class="body">'+esc(p.active_summary||'当前没有运行中的任务')+'</div></div>'}
function taskCard(x){var body=x.runtime||x.error_message||x.latest||'暂无详细信息';var acts='';if(x.actions.promote)acts+='<button class="btn secondary" data-action="promote" data-task-id="'+x.id+'">插队到 P0</button>';if(x.actions.retry)acts+='<button class="btn warn" data-action="retry" data-task-id="'+x.id+'">重试</button>';if(x.actions.stop)acts+='<button class="btn danger" data-action="stop" data-task-id="'+x.id+'">停止</button>';var inspect=x.source==='auto-inspect'?'<span class="tag status-failed">巡检建议</span>':'';return '<article class="task '+(S.taskId===x.id?'sel':'')+'" data-task="'+x.id+'"><div class="split"><div><strong>#'+x.id+' '+esc(x.title)+'</strong><div class="meta"><span class="tag '+stsCls(x.status)+'">'+esc(x.status)+'</span><span class="tag status-backlog">'+esc(x.priority)+'</span><span class="tag status-in_progress">'+esc(x.agent||'-')+'</span><span class="tag status-backlog">重试 '+(x.retry_count||0)+'/'+(x.max_retries||0)+'</span>'+inspect+'</div><div class="meta"><span>阶段: '+esc(x.phase||'-')+'</span><span>开始: '+esc(fmtTime(x.started_at))+'</span><span>完成: '+esc(fmtTime(x.completed_at))+'</span></div></div></div><div class="body">'+esc(body)+'</div><div class="split" style="margin-top:10px">'+acts+'</div></article>'}
function renderList(id,items,empty){$(id).innerHTML=items.length?items.map(taskCard).join(''):'<div class="empty">'+esc(empty)+'</div>'}
function bindTaskClicks(){document.querySelectorAll('[data-task]').forEach(function(n){n.onclick=function(ev){if(ev.target.closest('[data-action]'))return;S.taskId=Number(n.dataset.task);loadTaskDetail()}})}
function bindActions(){document.querySelectorAll('[data-action]').forEach(function(b){b.onclick=async function(ev){ev.preventDefault();ev.stopPropagation();setLoading(b,true);try{var out=await postj('/api/tasks/'+b.dataset.taskId+'/'+b.dataset.action,{});flash(out.message||'操作完成','success');await loadDashboard()}catch(err){flash(err.message,'error')}finally{setLoading(b,false,b.dataset.action)}}})}
function bindProjects(){document.querySelectorAll('[data-project]').forEach(function(n){n.onclick=function(){S.project=n.dataset.project;S.taskId=null;loadDashboard();loadSessions()}})}
function syncMode(){var m=$('mode').value;$('plannerWrap').style.display=m==='requirement'?'flex':'none';$('executeNow').disabled=m==='task';if(m==='task')$('executeNow').checked=false}
function pickTask(tasks){return (tasks.find(function(x){return x.status==='in_progress'})||tasks.find(function(x){return x.status==='failed'})||tasks[0]||{}).id||null}

/* --- View switching --- */
function switchView(view){S.view=view;document.querySelectorAll('.tab').forEach(function(el){el.classList.toggle('active',el.dataset.view===view)});$('dashboardView').classList.toggle('active',view==='dashboard');$('chatView').classList.toggle('active',view==='chat');if(view==='chat')loadSessionChat()}
document.querySelectorAll('.tab').forEach(function(el){el.onclick=function(){switchView(el.dataset.view)}});

/* --- Sessions (cross-project) --- */
async function loadSessions(){
  try{
    var url=S.project?'/api/sessions?project='+encodeURIComponent(S.project):'/api/sessions';
    var data=await getj(url);
    var list=data.sessions||[];
    if(!list.length){$('sessions').innerHTML='<div class="empty">暂无会话</div>';return}
    var html=list.map(function(s){
      var isActive=S.sessionId===s.id;
      var projLabel=s.project!==S.project?'<span class="session-project-label">'+esc(s.project)+'</span>':'';
      return '<div class="session-item '+(isActive?'active':'')+'" data-session="'+s.id+'" data-session-project="'+esc(s.project)+'"><div class="split"><strong style="font-size:13px">'+esc(s.title)+'</strong><span class="muted" style="font-size:11px">'+(s.message_count||0)+' 条</span></div><div class="muted" style="font-size:11px;margin-top:2px">'+esc(fmtTime(s.updated_at))+' '+projLabel+'</div></div>'
    }).join('');
    $('sessions').innerHTML=html;
    document.querySelectorAll('[data-session]').forEach(function(n){n.onclick=function(){
      S.sessionId=Number(n.dataset.session);
      loadSessions();
      if(S.view==='chat')loadSessionChat();else switchView('chat');
    }});
  }catch(err){$('sessions').innerHTML='<div class="empty">'+esc(err.message)+'</div>'}
}

$('newSessionBtn').onclick=async function(){
  if(!S.project){flash('先选择一个项目','error');return}
  var btn=$('newSessionBtn');setLoading(btn,true);
  try{var out=await postj('/api/sessions',{project:S.project,title:''});S.sessionId=out.session.id;await loadSessions();switchView('chat');flash('会话已创建','success')}
  catch(err){flash(err.message,'error')}finally{setLoading(btn,false,'+ 新建')}
};

async function loadSessionChat(){
  var box=$('chatMessages'),bar=$('chatInputBar'),del=$('deleteSessionBtn');
  if(!S.sessionId){box.innerHTML='<div class="empty">从左侧选择一个会话，或点击「+ 新建」开始。</div>';bar.style.display='none';del.style.display='none';$('chatSessionTitle').textContent='选择或新建一个会话';$('chatSessionMeta').textContent='';return}
  try{
    var data=await getj('/api/sessions/'+S.sessionId);var s=data.session;var msgs=data.messages||[];
    $('chatSessionTitle').textContent='#'+s.id+' '+s.title;
    $('chatSessionMeta').textContent='项目: '+s.project+' | 创建: '+fmtTime(s.created_at);
    del.style.display='inline-flex';bar.style.display='flex';
    if(!msgs.length){box.innerHTML='<div class="empty">会话刚创建，发送第一条消息开始对话。</div>'}
    else{box.innerHTML=msgs.map(function(m){
      var intent=m.intent?'<span class="tag '+stsCls(m.intent)+'" style="font-size:10px;padding:2px 6px">'+esc(m.intent)+'</span>':'';
      var tasks=m.task_ids&&m.task_ids.length?' <span class="muted" style="font-size:11px">任务: '+m.task_ids.map(function(id){return '#'+id}).join(', ')+'</span>':'';
      return '<div class="chat-msg '+esc(m.role)+'"><div>'+esc(m.content)+'</div><div class="msg-meta">'+esc(fmtTime(m.created_at))+' '+intent+tasks+'</div></div>'
    }).join('');box.scrollTop=box.scrollHeight}
  }catch(err){box.innerHTML='<div class="empty">'+esc(err.message)+'</div>'}
}

$('chatSend').onclick=async function(){
  if(!S.sessionId)return;
  var input=$('chatInput');var text=input.value.trim();if(!text)return;
  var cat=$('chatCategory').value;var btn=$('chatSend');setLoading(btn,true);
  try{await postj('/api/sessions/'+S.sessionId+'/messages',{text:text,category:cat});input.value='';await loadSessionChat();await loadSessions();flash('消息已发送','success')}
  catch(err){flash(err.message,'error')}finally{setLoading(btn,false,'发送')}
};
$('chatInput').onkeydown=function(ev){if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();$('chatSend').click()}};
$('deleteSessionBtn').onclick=async function(){
  if(!S.sessionId)return;if(!confirm('确定要删除这个会话吗？'))return;
  var btn=$('deleteSessionBtn');setLoading(btn,true);
  try{await delj('/api/sessions/'+S.sessionId);S.sessionId=null;await loadSessions();loadSessionChat();flash('会话已删除','success')}
  catch(err){flash(err.message,'error')}finally{setLoading(btn,false,'删除会话')}
};

/* --- Task detail --- */
async function loadTaskDetail(){
  var box=$('taskDetail');
  if(!S.taskId){box.innerHTML='<div class="empty">先选择一个任务。</div>';return}
  try{
    var x=await getj('/api/tasks/'+S.taskId);
    var deps=x.depends_on&&x.depends_on.length?x.depends_on.map(function(id){return '<span class="tag status-backlog">#'+id+'</span>'}).join(' '):'<span class="muted">无依赖</span>';
    var logs=x.logs&&x.logs.length?'<div class="detail"><div class="muted">阶段日志摘要</div><pre>'+esc(x.logs.map(function(i){return '['+( i.phase||'-')+'] agent='+(i.agent||'-')+' exit='+(i.exit_code==null?'-':i.exit_code)+'\n'+(i.output_excerpt||'')}).join('\n\n'))+'</pre></div>':'';
    var fail=x.error_message?'<div class="detail"><div class="muted">失败原因</div><pre>'+esc(x.error_message)+'</pre></div>':'';
    var delivery=x.delivery_record?'<div class="detail"><div class="muted">交付记录</div><pre>'+esc(x.delivery_record)+'</pre></div>':'';
    var acts='';
    if(x.actions.promote)acts+='<button class="btn secondary" data-action="promote" data-task-id="'+x.id+'">插队到 P0</button>';
    if(x.actions.retry)acts+='<button class="btn warn" data-action="retry" data-task-id="'+x.id+'">重试</button>';
    if(x.actions.stop)acts+='<button class="btn danger" data-action="stop" data-task-id="'+x.id+'">停止</button>';
    box.innerHTML='<div class="detail"><div class="split"><div><strong>#'+x.id+' '+esc(x.title)+'</strong><div class="meta"><span class="tag '+stsCls(x.status)+'">'+esc(x.status)+'</span><span class="tag status-backlog">'+esc(x.priority)+'</span><span class="tag status-in_progress">'+esc(x.agent||'-')+'</span><span class="tag status-backlog">重试 '+(x.retry_count||0)+'/'+(x.max_retries||0)+'</span></div></div><div class="split">'+acts+'</div></div><div class="meta"><span>项目: '+esc(x.project)+'</span><span>阶段: '+esc(x.phase||'-')+'</span><span>运行态: '+esc(x.runtime||'-')+'</span></div><div class="meta"><span>依赖: '+deps+'</span></div><div class="meta"><span>项目路径: '+esc(x.project_path||'-')+'</span></div><div class="meta"><span>日志文件: '+esc(x.current_log_path||'-')+'</span></div></div>'+fail+'<div class="detail"><div class="muted">任务内容</div><pre>'+esc(x.content||'暂无任务内容')+'</pre></div><div class="detail"><div class="muted">实时日志 / 最近输出</div><pre>'+esc(x.log_text||'还没有可显示的日志')+'</pre></div>'+delivery+logs;
    bindActions()
  }catch(err){box.innerHTML='<div class="empty">'+esc(err.message)+'</div>'}
}

/* --- Dashboard --- */
async function loadDashboard(){
  if(S.busy)return;
  try{
    var data=await getj(S.project?'/api/projects/'+encodeURIComponent(S.project):'/api/projects');
    S.project=data.selected_project;
    $('projects').innerHTML=(data.projects||[]).length?data.projects.map(projectCard).join(''):'<div class="empty">还没有项目。先执行一次 codepilot init。</div>';
    bindProjects();
    $('jobs').innerHTML=(data.jobs||[]).length?data.jobs.map(function(j){var isActive=j.status==='running'||j.status==='queued';var spinner=isActive?'<span class="job-spinner"></span> ':'';var phaseLabel={'queued':'排队中','planning':'规划中...','running':'执行中...','done':'完成','failed':'失败','attention':'需关注'}[j.phase]||j.phase;var logHtml='';if(j.log&&j.log.length){var recent=j.log.slice(-5);logHtml='<pre class="job-log">'+esc(recent.join('\n'))+'</pre>'}var bodyText=j.summary||j.error||logHtml||'等待中';if(j.summary||j.error)bodyText=esc(j.summary||j.error);else if(logHtml)bodyText=logHtml;else bodyText=esc('等待中');return '<div class="job'+(isActive?' job-active':'')+'"><div class="split"><strong>'+spinner+'#'+j.id+' '+esc(j.title)+'</strong><span class="tag '+stsCls(j.status)+'">'+esc(phaseLabel)+'</span></div><div class="meta"><span>planner: '+esc(j.planner||'-')+'</span><span>agent: '+esc(j.agent||'auto')+'</span></div><div class="body">'+bodyText+'</div></div>'}).join(''):'<div class="empty">还没有从 Web UI 发起的需求。</div>';
    $('events').innerHTML=(data.events||[]).length?data.events.map(function(ev){return '<div class="event"><div class="split"><span class="tag '+stsCls(ev.level||'info')+'">'+esc(ev.level||'info')+'</span><span class="muted">'+esc(fmtTime(ev.time))+'</span></div><div class="body">'+esc(ev.message)+'</div></div>'}).join(''):'<div class="empty">最近还没有事件。</div>';
    var p=(data.projects||[]).find(function(i){return i.name===data.selected_project});
    $('projectTitle').textContent=p?p.name:'项目总览';
    $('projectPath').textContent=p?p.path:'当前没有已注册项目';
    var st=p?p.stats:{in_progress:0,backlog:0,failed:0,cancelled:0,done:0,total:0};
    $('metrics').innerHTML=[metric('进行中',st.in_progress||0),metric('待办',st.backlog||0),metric('失败 / 取消',(st.failed||0)+(st.cancelled||0)),metric('已完成',st.done||0),metric('总任务',st.total||0)].join('');
    var tasks=data.tasks||[];
    renderList('running',tasks.filter(function(x){return x.status==='in_progress'}),'当前没有运行中的任务');
    renderList('backlog',tasks.filter(function(x){return x.status==='backlog'}),'当前 backlog 为空');
    renderList('failed',tasks.filter(function(x){return x.status==='failed'||x.status==='cancelled'}),'当前没有失败或取消的任务');
    renderList('done',tasks.filter(function(x){return x.status==='done'}).slice(0,8),'还没有已完成任务');
    bindTaskClicks();bindActions();
    var ids=tasks.map(function(x){return x.id});
    if(!S.taskId&&tasks.length)S.taskId=pickTask(tasks);
    else if(S.taskId&&ids.indexOf(S.taskId)===-1)S.taskId=pickTask(tasks);
    await loadTaskDetail();
    await loadSessions()
  }catch(err){flash(err.message,'error')}
}

function schedule(){clearInterval(S.timer);if(!S.auto)return;S.timer=setInterval(function(){if(!S.busy)loadDashboard()},3000)}

/* --- Event bindings --- */
$('refreshBtn').onclick=loadDashboard;
$('toggleBtn').onclick=function(){S.auto=!S.auto;$('toggleBtn').textContent='自动刷新：'+(S.auto?'开':'关');schedule()};
$('mode').onchange=syncMode;

$('composer').onsubmit=async function(ev){
  ev.preventDefault();
  if(!S.project){flash('当前没有已注册项目，先执行一次 codepilot init。','error');return}
  var title=$('titleInput').value.trim();
  if(!title){flash('标题不能为空。','error');return}
  var btn=$('composerSubmit');setLoading(btn,true);S.busy=true;
  var payload={project:S.project,title:title,content:$('contentInput').value,priority:$('priority').value,agent:$('agent').value,planner:$('planner').value,execute:$('executeNow').checked};
  try{
    var mode=$('mode').value;var out;
    if(mode==='task'){out=await postj('/api/tasks',payload);S.taskId=out.task.id;$('contentInput').value=''}
    else{out=await postj('/api/requirements',payload)}
    $('titleInput').value='';
    flash(out.message||'提交成功','success');
    await loadDashboard()
  }catch(err){flash(err.message,'error')}finally{setLoading(btn,false,'提交到当前项目');S.busy=false}
};

$('goalSubmit').onclick=async function(){
  if(!S.project){flash('当前没有已注册项目，先执行一次 codepilot init。','error');return}
  var text=$('goalText').value.trim();
  if(!text){flash('输入不能为空。','error');return}
  var cat=$('goalCategory').value;
  var btn=$('goalSubmit');setLoading(btn,true);S.busy=true;
  flashAnswer('');
  try{
    var out=await postj('/api/goal',{project:S.project,text:text,category:cat});
    if(out.intent==='question'||out.intent==='command'){flashAnswer(out.message||'完成')}
    else{flash(out.message||'提交成功','success');await loadDashboard()}
    $('goalText').value=''
  }catch(err){flash(err.message,'error')}finally{setLoading(btn,false,'提交');S.busy=false}
};
$('goalText').onkeydown=function(ev){if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();$('goalSubmit').click()}};

/* --- Init --- */
syncMode();loadDashboard();schedule();
</script></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodePilotUI/0.2"

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("请求体不是合法 JSON。") from exc

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(HTML)
            return
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        if path == "/api/projects":
            self._send_json(dashboard_payload())
            return
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            self._send_json(dashboard_payload(unquote(match.group(1))))
            return
        match = re.fullmatch(r"/api/tasks/(\d+)", path)
        if match:
            try:
                self._send_json(task_detail_payload(int(match.group(1))))
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=404)
            return
        # Session endpoints
        match = re.fullmatch(r"/api/sessions", path)
        if match:
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            proj = qs.get("project", [""])[0]
            try:
                self._send_json(list_sessions_action(proj))
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        match = re.fullmatch(r"/api/sessions/(\d+)", path)
        if match:
            try:
                self._send_json(get_session_action(int(match.group(1))))
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=404)
            return
        self._send_json({"error": "未找到页面。"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/goal":
                body = self._read_json_body()
                self._send_json(
                    submit_goal_action(
                        body.get("project") or "",
                        body.get("text") or "",
                        category=body.get("category") or "auto",
                    )
                )
                return
            if path == "/api/tasks":
                body = self._read_json_body()
                self._send_json(
                    create_task_action(
                        body.get("project") or "",
                        body.get("title") or "",
                        content=body.get("content") or "",
                        priority=body.get("priority") or "P2",
                        agent=body.get("agent") or None,
                        max_retries=int(body.get("max_retries") or 3),
                    )
                )
                return
            if path == "/api/requirements":
                body = self._read_json_body()
                self._send_json(
                    submit_requirement_action(
                        body.get("project") or "",
                        body.get("title") or "",
                        execute=bool(body.get("execute", True)),
                        planner=body.get("planner") or "codex",
                        agent=None if body.get("agent") in {"", None, "auto"} else body.get("agent"),
                        priority=body.get("priority") or "P2",
                        max_tasks=int(body.get("max_tasks") or 5),
                        executor=body.get("executor") or "auto",
                        auto_commit=bool(body.get("auto_commit", False)),
                        max_retries=int(body.get("max_retries") or 3),
                        run_async=bool(body.get("run_async", True)),
                    )
                )
                return
            match = re.fullmatch(r"/api/tasks/(\d+)/(retry|stop|promote)", path)
            if match:
                task_id = int(match.group(1))
                action = match.group(2)
                if action == "retry":
                    payload = retry_task_action(task_id)
                elif action == "stop":
                    payload = stop_task_action(task_id)
                else:
                    payload = promote_task_action(task_id)
                self._send_json(payload)
                return
            # Session POST endpoints
            if path == "/api/sessions":
                body = self._read_json_body()
                self._send_json(
                    create_session_action(
                        body.get("project") or "",
                        title=body.get("title") or "",
                    )
                )
                return
            match = re.fullmatch(r"/api/sessions/(\d+)/messages", path)
            if match:
                body = self._read_json_body()
                self._send_json(
                    send_session_message_action(
                        int(match.group(1)),
                        body.get("text") or "",
                        category=body.get("category") or "auto",
                    )
                )
                return
        except (RuntimeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json({"error": "未找到接口。"}, status=404)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            match = re.fullmatch(r"/api/sessions/(\d+)", path)
            if match:
                self._send_json(delete_session_action(int(match.group(1))))
                return
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json({"error": "未找到接口。"}, status=404)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def start_ui_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = True,
    browser_opener: Callable[[str], bool] | None = None,
) -> ThreadingHTTPServer:
    db.init_db()
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    if open_browser:
        opener = browser_opener or webbrowser.open
        threading.Timer(0.3, lambda: opener(f"http://{host}:{port}/")).start()
    return server
