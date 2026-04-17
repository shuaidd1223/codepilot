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


_WEB_DIR = Path(__file__).parent / "web"


def _load_web_file(name: str) -> bytes:
    path = _WEB_DIR / name
    return path.read_bytes()


def _safe_web_path(rel: str) -> Path | None:
    """Resolve *rel* under _WEB_DIR, preventing path traversal."""
    try:
        candidate = (_WEB_DIR / rel).resolve()
        base = _WEB_DIR.resolve()
        candidate.relative_to(base)
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodePilotUI/0.2"

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_html_file(self, name: str = "index.html") -> None:
        try:
            body = _load_web_file(name)
        except FileNotFoundError:
            self._send_json({"error": f"找不到 {name}"}, status=500)
            return
        self._send_bytes(body, "text/html; charset=utf-8")

    def _serve_static(self, rel: str) -> bool:
        target = _safe_web_path(rel)
        if not target:
            return False
        content_type = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send_bytes(target.read_bytes(), content_type)
        return True

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
            self._send_html_file()
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if self._serve_static(rel):
                return
            self._send_json({"error": "未找到文件。"}, status=404)
            return
        if path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
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
