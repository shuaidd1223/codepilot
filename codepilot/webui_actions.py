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
from pathlib import Path
from typing import Optional

from codepilot import db
from codepilot.commands.auto import clarify_requirement  # noqa: F401 — patched in tests
from codepilot.commands.init import initialize_project
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


def create_project_action(path: str, *, name: str = "", no_config: bool = False) -> dict:
    db.init_db()
    raw_path = (path or "").strip().strip('"')
    if not raw_path:
        raise RuntimeError("工作目录不能为空。")
    result = initialize_project(Path(raw_path).expanduser(), name.strip() or None, no_config=no_config)
    project = result["project"]
    action = "注册" if result["created"] else "更新"
    _append_event(f"{action}项目：{project['name']}", project=project["name"])
    return {
        "ok": True,
        "created": bool(result["created"]),
        "message": f"项目 '{project['name']}' 已{action}。",
        "project": project,
        "config_file": result.get("config_file") or "",
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


def retry_task_action(task_id: int) -> dict:
    """Retry a task: reset it to backlog AND kick off one execution pass in
    a background thread so the UI feels like "click retry → task starts".

    Before, retry only reset the DB row and waited for an external daemon
    (or a manual ``codepilot run``) to pick it up. Without a running
    daemon, nothing happened — the user saw a green toast but the task
    sat idle. The background :func:`run_backlog` call below is a single-
    pass run (``once=True``, ``limit=1``) so it processes at most one
    runnable task and exits; its own live-task guard ensures it no-ops
    cleanly if a daemon is already running something.
    """
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    if task["status"] == "in_progress":
        raise RuntimeError(f"任务 #{task_id} 正在运行中，请先停止再重试。")
    if task["status"] == "done":
        raise RuntimeError(f"任务 #{task_id} 已完成，不能直接重试。")
    updated = db.reset_task_for_retry(task_id)
    project_name = task["project"]
    _append_event(f"任务 #{task_id} 已重试，后台开始执行…", project=project_name, task_id=task_id)

    def _run_worker() -> None:
        try:
            from codepilot.commands.run import run_backlog
            run_backlog(project_name, once=True, limit=1, quiet=True)
        except Exception as exc:
            _append_event(
                f"任务 #{task_id} 后台执行失败：{exc}",
                level="error",
                project=project_name,
                task_id=task_id,
            )

    threading.Thread(
        target=_run_worker,
        name=f"codepilot-ui-retry-{task_id}",
        daemon=True,
    ).start()

    return {
        "ok": True,
        "message": f"任务 #{task_id} 已重试，后台开始执行。",
        "task": _task_payload(updated),
    }


def promote_task_action(task_id: int) -> dict:
    """Promote a task to P0 and kick off a run, mirroring retry semantics.

    Without the background kicker, a promote click also just sits in
    backlog until something drains the queue. Since the user's intent is
    clearly "run this next", start one run-pass right after bumping
    priority.
    """
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
    project_name = task["project"]
    _append_event(f"任务 #{task_id} 已插队到 P0，后台开始执行…", project=project_name, task_id=task_id)

    def _run_worker() -> None:
        try:
            from codepilot.commands.run import run_backlog
            run_backlog(project_name, once=True, limit=1, quiet=True)
        except Exception as exc:
            _append_event(
                f"任务 #{task_id} 后台执行失败：{exc}",
                level="error",
                project=project_name,
                task_id=task_id,
            )

    threading.Thread(
        target=_run_worker,
        name=f"codepilot-ui-promote-{task_id}",
        daemon=True,
    ).start()

    return {
        "ok": True,
        "message": f"任务 #{task_id} 已提升到 P0 并开始执行。",
        "task": _task_payload(updated),
    }


def split_task_action(task_id: int) -> dict:
    """Re-plan an existing task into smaller subtasks.

    The existing task becomes ``cancelled`` (so history is kept) and the
    planner is asked to break its original title / content into concrete
    subtasks. Useful when a task turned out to be too big to tackle in one
    go — the user clicks "拆分" and gets a fresh, finer-grained backlog.
    """
    db.init_db()
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(f"任务 #{task_id} 不存在。")
    project = task["project"]
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    title = task["title"] or ""
    # Inline part of the content as additional context for the planner so
    # the resulting subtasks preserve the original intent (goal / acceptance
    # criteria). We intentionally append rather than replace the title.
    content_hint = (task.get("content") or "").strip()
    if content_hint:
        expanded_title = f"{title}（拆分更细；原任务内容摘要：{content_hint[:400]}）"
    else:
        expanded_title = title

    # Cancel the original so the dashboard reflects the reshape.
    db.update_task(
        task_id,
        status="cancelled",
        error_message="由用户请求拆分为更小任务，原任务已关闭",
    )
    _append_event(
        f"任务 #{task_id} 已请求拆分，原任务关闭",
        level="warning", project=project, task_id=task_id,
    )

    result = submit_requirement_action(
        project,
        expanded_title,
        execute=False,
        max_tasks=5,
        run_async=True,
    )
    result["intent"] = "split"
    result["original_task_id"] = task_id
    result["message"] = f"任务 #{task_id} 已关闭，正在规划新的细粒度任务（后台任务 #{result.get('job', {}).get('id')}）。"
    return result


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
    resolved_agent = (agent or project_info.get("default_mode") or "dual").lower()
    if resolved_agent == "auto":
        resolved_agent = project_info.get("default_mode") or "dual"

    from codepilot.ai import resolve_agent_with_fallback
    default_mode = project_info.get("default_mode") or "dual"
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
        from codepilot import progress_bus

        _update_job(job_id, status="running", phase="planning", updated_at=_now_iso())
        _append_job_log(f"开始规划：{normalized_title}")
        _append_job_log(f"使用规划器：{planner}")

        # Subscribe this job to the progress bus. Every event emitted during
        # this worker's run — regardless of whether it comes from the planner,
        # recon stage, or the builder/reviewer loop — lands in the job log and
        # flows out to any SSE listeners attached to the bus as well.
        def _bus_listener(event: dict) -> None:
            stage = event.get("stage") or "?"
            message = event.get("message") or ""
            extra = event.get("extra") or {}
            round_hint = ""
            if "round" in extra and "round_total" in extra:
                round_hint = f" (round {extra['round']}/{extra['round_total']})"
            _append_job_log(f"[{stage}{round_hint}] {message}")

        # Keep the old planner callback mirror for now so any code still
        # reading ai_mod._planner_progress_callback (e.g. very old subscribers)
        # doesn't regress.
        prev_callback = _ai_module._planner_progress_callback
        _ai_module._planner_progress_callback = _append_job_log

        with progress_bus.subscription(_bus_listener):
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


def submit_goal_action(
    project: str,
    text: str,
    *,
    category: str = "auto",
    qa_history: Optional[list[dict]] = None,
    original_title: str = "",
) -> dict:
    """POST /api/goal — classify intent and route accordingly.

    *category* can be ``auto``, ``question``, ``requirement``, or ``command``.

    For multi-turn clarification, callers pass ``original_title`` and
    ``qa_history``; the action threads them through :func:`clarify_requirement`
    and either returns a new ``clarify`` response or starts planning.
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
        base_url = None
        if classifier_cfg and classifier_cfg.enabled:
            if classifier_cfg.provider:
                api_key = cfg.get_provider_api_key(classifier_cfg.provider)
                provider_cfg = cfg.providers.get(classifier_cfg.provider)
                base_url = provider_cfg.base_url if provider_cfg else None
            try:
                result = classify_intent(
                    text,
                    project_path=project_info["path"],
                    classifier_provider=classifier_cfg.provider,
                    classifier_model=classifier_cfg.model,
                    timeout=classifier_cfg.timeout,
                    api_key=api_key,
                    base_url=base_url,
                )
                intent = result["intent"]
            except Exception:
                intent = "requirement"
        else:
            intent = "requirement"
    else:
        intent = category

    # Mid-clarification: treat the new text as the user's answer to the prior
    # round and skip re-classification.
    if original_title:
        intent = "requirement"

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
        provider_cfg = cfg.providers.get(provider_key) if provider_key else None
        base_url = provider_cfg.base_url if provider_cfg else None
        try:
            answer = answer_question_via_api(
                provider_key=provider_key,
                question=text,
                project_path=project_info["path"],
                model_override=classifier_cfg.model if classifier_cfg else "",
                api_key=api_key,
                base_url=base_url,
            )
        except Exception as exc:
            answer = f"回答失败：{exc}"
        _append_event(f"回答问题：{text[:60]}", project=project)
        return {"ok": True, "intent": "question", "message": answer or "未获得回答"}

    # ── Clarification (multi-turn) gate before actually planning ──────────
    if original_title:
        merged_history = list(qa_history or []) + [{"question": "", "answer": text}]
        seed_title = original_title
    else:
        merged_history = []
        seed_title = text

    assessment = clarify_requirement(
        seed_title,
        project_info=project_info,
        qa_history=merged_history,
    )

    if assessment.get("status") == "needs_clarification":
        _append_event(f"需求需要澄清：{seed_title[:60]}", project=project)
        return {
            "ok": True,
            "intent": "clarify",
            "questions": assessment.get("questions") or [],
            "original_title": seed_title,
            "qa_history": assessment.get("qa_history") or [],
            "message": "为了更好地规划，请先回答几个问题。",
        }

    refined = assessment.get("refined_title") or seed_title
    max_tasks = 1 if intent == "task" else 5
    result = submit_requirement_action(
        project,
        refined,
        execute=True,
        max_tasks=max_tasks,
        run_async=True,
    )
    result["intent"] = intent
    result["refined_title"] = refined
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


def _reconstruct_clarification_state(messages: list[dict]) -> Optional[dict]:
    """Walk session messages and, if the last assistant turn is a clarify
    request, rebuild ``{original_title, qa_history, last_questions}``.

    Returns ``None`` when the session is not mid-clarification.
    """
    if not messages:
        return None
    # The last *assistant* message determines whether we're waiting on answers.
    last_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break
    if last_assistant_idx is None:
        return None
    last_assistant = messages[last_assistant_idx]
    if last_assistant.get("intent") != "clarify":
        return None

    # Walk back to the first user message of this clarification burst.
    clarify_start = last_assistant_idx
    while clarify_start - 2 >= 0:
        prev_assistant = messages[clarify_start - 2]
        if prev_assistant.get("role") == "assistant" and prev_assistant.get("intent") == "clarify":
            clarify_start -= 2
            continue
        break

    original_user_idx = clarify_start - 1
    if original_user_idx < 0 or messages[original_user_idx].get("role") != "user":
        return None

    original_title = (messages[original_user_idx].get("content") or "").strip()
    # Collect Q/A pairs between the original user message and *last_assistant*
    # (exclusive on the assistant at last_assistant_idx because that one is the
    # *outstanding* question the current user input answers).
    qa_history: list[dict] = []
    # Iterate paired (assistant clarify, user answer) through prior rounds.
    idx = clarify_start
    while idx < last_assistant_idx:
        a_msg = messages[idx]
        u_msg = messages[idx + 1] if idx + 1 < len(messages) else None
        if (
            a_msg.get("role") == "assistant" and a_msg.get("intent") == "clarify"
            and u_msg and u_msg.get("role") == "user"
        ):
            qa_history.append({
                "question": (a_msg.get("content") or "").strip(),
                "answer": (u_msg.get("content") or "").strip(),
            })
            idx += 2
        else:
            break

    last_questions_raw = (last_assistant.get("content") or "").splitlines()
    last_questions = [line.lstrip("0123456789.、 -") for line in last_questions_raw if line.strip()]

    return {
        "original_title": original_title,
        "qa_history": qa_history,
        "last_questions": last_questions,
    }


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

    # ── Multi-turn clarification: was the previous assistant turn a clarify? ──
    pending = _reconstruct_clarification_state(existing_messages)
    if pending and category in {"auto", "", None}:
        db.create_session_message(session_id, "user", text)
        merged_history = list(pending["qa_history"])
        last_qs = pending.get("last_questions") or []
        merged_history.append({
            "question": " | ".join(last_qs),
            "answer": text,
        })
        assessment = clarify_requirement(
            pending["original_title"],
            project_info=project_info,
            qa_history=merged_history,
        )
        if assessment.get("status") == "needs_clarification":
            questions = assessment.get("questions") or []
            reply = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            db.create_session_message(session_id, "assistant", reply, intent="clarify")
            return {
                "ok": True,
                "intent": "clarify",
                "message": reply,
                "questions": questions,
                "task_ids": [],
            }
        refined = assessment.get("refined_title") or pending["original_title"]
        plan_result = submit_requirement_action(
            project, refined, execute=True, max_tasks=5, run_async=True,
        )
        task_ids = (plan_result.get("job") or {}).get("task_ids") or []
        reply = plan_result.get("message") or f"已根据澄清结果开始规划：{refined}"
        db.create_session_message(
            session_id, "assistant", reply, intent="requirement", task_ids=task_ids,
        )
        return {
            "ok": True,
            "intent": "requirement",
            "message": reply,
            "refined_title": refined,
            "task_ids": task_ids,
        }

    db.create_session_message(session_id, "user", text)

    category = (category or "auto").lower()
    if category == "auto":
        cfg = load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))
        classifier_cfg = getattr(cfg, "classifier", None)
        api_key = None
        base_url = None
        if classifier_cfg and classifier_cfg.enabled:
            if classifier_cfg.provider:
                api_key = cfg.get_provider_api_key(classifier_cfg.provider)
                provider_cfg = cfg.providers.get(classifier_cfg.provider)
                base_url = provider_cfg.base_url if provider_cfg else None
            try:
                result = classify_intent(
                    text,
                    project_path=project_info["path"],
                    classifier_provider=classifier_cfg.provider,
                    classifier_model=classifier_cfg.model,
                    timeout=classifier_cfg.timeout,
                    api_key=api_key,
                    base_url=base_url,
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
        provider_cfg = cfg.providers.get(provider_key) if provider_key else None
        base_url = provider_cfg.base_url if provider_cfg else None
        try:
            answer = answer_question_via_api(
                provider_key=provider_key,
                question=text,
                project_path=project_info["path"],
                model_override=classifier_cfg.model if classifier_cfg else "",
                api_key=api_key,
                base_url=base_url,
            )
        except Exception as exc:
            answer = f"回答失败：{exc}"
        reply = answer or "未获得回答"
        db.create_session_message(session_id, "assistant", reply, intent="question")
        return {"ok": True, "intent": "question", "message": reply, "task_ids": []}

    assessment = clarify_requirement(text, project_info=project_info, qa_history=[])
    if assessment.get("status") == "needs_clarification":
        questions = assessment.get("questions") or []
        reply = "为了更好地规划，请先确认以下几个点：\n" + "\n".join(
            f"{i}. {q}" for i, q in enumerate(questions, 1)
        )
        db.create_session_message(session_id, "assistant", reply, intent="clarify")
        return {
            "ok": True,
            "intent": "clarify",
            "message": reply,
            "questions": questions,
            "task_ids": [],
        }

    refined = assessment.get("refined_title") or text
    max_tasks = 1 if intent == "task" else 5
    result = submit_requirement_action(project, refined, execute=True, max_tasks=max_tasks, run_async=True)
    task_ids = []
    job = result.get("job")
    if job:
        task_ids = job.get("task_ids") or []
    reply = result.get("message") or "需求已提交"
    db.create_session_message(session_id, "assistant", reply, intent=intent, task_ids=task_ids)
    return {"ok": True, "intent": intent, "message": reply, "refined_title": refined, "task_ids": task_ids}


def delete_session_action(session_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    db.delete_session(session_id)
    _append_event(f"删除会话 #{session_id}", project=session["project"])
    return {"ok": True, "message": f"会话 #{session_id} 已删除。"}
