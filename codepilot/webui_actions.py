"""State-mutating actions that back the Web UI HTTP endpoints.

Imported and re-exported by :mod:`codepilot.webui`. The shared UI state
(``_UI_JOBS``, ``_UI_EVENTS``, ``_UI_JOB_SEQ``, ``_UI_LOCK``) lives on the shell
module so tests can reset it directly (e.g. ``webui_mod._UI_JOBS.clear()``,
``webui_mod._UI_JOB_SEQ = 0``); every function in here reaches that state and
the patchable ``run_requirement_workflow`` via ``codepilot.webui``.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Optional

from codepilot import db
from codepilot.config import load_project_config
from codepilot.webui_payloads import _now_iso, _task_payload


_MAX_EVENTS = 40
_MAX_JOB_LOG_LINES = 50
_GOAL_MAX_BYTES = 4096


def _shell():
    """Return the ``codepilot.webui`` shell module for state/dependency access."""
    return sys.modules["codepilot.webui"]


def _append_event(message: str, *, level: str = "info", project: str | None = None, task_id: int | None = None) -> None:
    shell = _shell()
    entry = {
        "time": _now_iso(),
        "level": level,
        "project": project or "",
        "task_id": task_id,
        "message": message,
    }
    with shell._UI_LOCK:
        shell._UI_EVENTS.append(entry)
        del shell._UI_EVENTS[:-_MAX_EVENTS]


def _next_job_id() -> int:
    shell = _shell()
    with shell._UI_LOCK:
        shell._UI_JOB_SEQ += 1
        return shell._UI_JOB_SEQ


def _update_job(job_id: int, **fields) -> dict:
    shell = _shell()
    with shell._UI_LOCK:
        job = shell._UI_JOBS[job_id]
        job.update(fields)
        return dict(job)


def list_ui_jobs(project: str | None = None) -> list[dict]:
    shell = _shell()
    with shell._UI_LOCK:
        items = [dict(job) for job in shell._UI_JOBS.values()]
    if project:
        items = [job for job in items if job.get("project") == project]
    items.sort(key=lambda item: (item.get("updated_at") or item.get("created_at") or "", item["id"]), reverse=True)
    return items[:12]


def list_ui_events(project: str | None = None) -> list[dict]:
    shell = _shell()
    with shell._UI_LOCK:
        items = list(shell._UI_EVENTS)
    if project:
        items = [event for event in items if not event.get("project") or event.get("project") == project]
    return list(reversed(items[-12:]))


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
    shell = _shell()
    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    normalized_title = " ".join((title or "").split())
    if not normalized_title:
        raise RuntimeError("需求文本不能为空。")
    job_id = _next_job_id()
    with shell._UI_LOCK:
        shell._UI_JOBS[job_id] = {
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
        with shell._UI_LOCK:
            job = shell._UI_JOBS.get(job_id)
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

        prev_callback = _ai_module._planner_progress_callback
        _ai_module._planner_progress_callback = _append_job_log
        try:
            result = shell.run_requirement_workflow(
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

    return {"ok": True, "message": f"需求已提交，后台任务 #{job_id} 已启动。", "job": dict(shell._UI_JOBS[job_id])}


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

    existing_messages = db.list_session_messages(session_id)
    if not existing_messages:
        short_title = text[:40] + ("…" if len(text) > 40 else "")
        db.update_session(session_id, title=short_title)

    db.create_session_message(session_id, "user", text)

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
