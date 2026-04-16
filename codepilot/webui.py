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
        }
    _append_event(f"收到需求：{normalized_title}", project=project)

    def worker() -> None:
        _update_job(job_id, status="running", phase="planning", updated_at=_now_iso())
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
            _update_job(
                job_id,
                status="failed",
                phase="failed",
                updated_at=_now_iso(),
                finished_at=_now_iso(),
                error=str(exc),
            )
            _append_event(f"需求执行失败：{normalized_title} | {exc}", level="error", project=project)

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


HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CodePilot 控制台</title>
<style>
:root{--bg:#f6f0e7;--panel:#fffdf8;--line:#e1d9cf;--text:#1f2933;--muted:#64707d;--brand:#0f6c9b;--brand-bg:#eaf6fb;--ok:#1b8b57;--ok-bg:#e9f8f1;--warn:#b87900;--warn-bg:#fff4de;--danger:#c43f52;--danger-bg:#fcebef;--shadow:0 18px 36px rgba(31,41,51,.10);font-family:"Segoe UI Variable","PingFang SC","Microsoft YaHei UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#f7f2ea,#edf2f6);color:var(--text)}
.shell{display:grid;grid-template-columns:300px 1fr;gap:20px;padding:20px;min-height:100vh}.side,.main{background:rgba(255,255,255,.9);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow);backdrop-filter:blur(14px)}
.side{padding:18px;display:flex;flex-direction:column;gap:16px}.main{padding:20px;display:flex;flex-direction:column;gap:16px}.title{margin:0;font-size:28px}.muted{color:var(--muted);line-height:1.5}
.toolbar,.split{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}.btn{border:0;border-radius:12px;padding:10px 14px;font:inherit;font-weight:700;cursor:pointer;background:var(--brand-bg);color:var(--brand)}
.btn.secondary{background:#f1f3f5;color:var(--text)}.btn.warn{background:var(--warn-bg);color:var(--warn)}.btn.danger{background:var(--danger-bg);color:var(--danger)}.btn.ok{background:var(--ok-bg);color:var(--ok)}
.card,.task,.job,.event,.detail{border:1px solid var(--line);border-radius:18px;background:var(--panel);padding:14px}.card.active,.task.sel{border-color:#9dccdf;background:#f4fbff}.list{display:flex;flex-direction:column;gap:10px}.scroll{max-height:calc(100vh - 260px);overflow:auto;padding-right:4px}
.tag{display:inline-flex;align-items:center;justify-content:center;padding:5px 10px;border-radius:999px;font-size:12px;font-weight:800}.status-backlog{background:#f1f3f5}.status-in_progress,.status-running{background:var(--brand-bg);color:var(--brand)}.status-failed,.status-cancelled,.status-error{background:var(--danger-bg);color:var(--danger)}.status-done,.status-succeeded{background:var(--ok-bg);color:var(--ok)}.status-warning,.status-attention{background:var(--warn-bg);color:var(--warn)}.status-queued{background:#f1f3f5}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.metric{border:1px solid var(--line);border-radius:16px;padding:14px;background:var(--panel)}.metric b{display:block;font-size:28px;margin-top:8px}
.hero,.workspace{display:grid;grid-template-columns:1fr 1fr;gap:16px}.workspace{grid-template-columns:1.1fr .9fr}.field{display:flex;flex-direction:column;gap:6px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.grid .wide{grid-column:1/-1}
input,select,textarea{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:12px;background:#fff;font:inherit;color:var(--text)}textarea{min-height:100px;resize:vertical;line-height:1.55}
.sections{display:flex;flex-direction:column;gap:16px}.task{cursor:pointer}.task:hover{border-color:#9dccdf}.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;color:var(--muted);font-size:12px}.body{margin-top:10px;white-space:pre-wrap;word-break:break-word;line-height:1.55}
.banner{display:none;padding:12px 14px;border-radius:14px;font-weight:700}.banner.show{display:block}.banner.info{background:var(--brand-bg);color:var(--brand)}.banner.success{background:var(--ok-bg);color:var(--ok)}.banner.error{background:var(--danger-bg);color:var(--danger)}
.empty{padding:18px;border:1px dashed var(--line);border-radius:14px;text-align:center;color:var(--muted)}pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:"Cascadia Code","Consolas",monospace;font-size:12px;line-height:1.6;max-height:300px;overflow:auto}
@media (max-width:1180px){.shell,.hero,.workspace{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.scroll{max-height:none}}@media (max-width:720px){.grid,.metrics{grid-template-columns:1fr}}
</style></head><body>
<div class="shell">
<aside class="side">
  <div><h1 class="title">CodePilot</h1><div class="muted">直接提需求，盯住任务、日志和失败原因。</div></div>
  <section><div class="muted">项目</div><div id="projects" class="list scroll"></div></section>
  <section><div class="muted">最近需求</div><div id="jobs" class="list"></div></section>
  <section><div class="muted">最近事件</div><div id="events" class="list"></div></section>
</aside>
<main class="main">
  <div class="toolbar"><div><h2 id="projectTitle" style="margin:0">项目总览</h2><div id="projectPath" class="muted">正在读取数据…</div></div><div class="split"><button id="refreshBtn" class="btn secondary">立即刷新</button><button id="toggleBtn" class="btn">自动刷新：开</button></div></div>
  <div id="goalBar" class="detail" style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
    <div class="field" style="flex:1;min-width:220px"><label>快速输入</label><input id="goalText" type="text" placeholder="输入问题、需求或命令…" maxlength="4096"></div>
    <div class="field" style="width:120px"><label>类型</label><select id="goalCategory"><option value="auto" selected>自动</option><option value="question">问题</option><option value="requirement">需求</option><option value="command">命令</option></select></div>
    <button id="goalSubmit" class="btn ok" style="height:42px;white-space:nowrap">提交</button>
  </div>
  <div id="goalAnswer" class="banner"></div>
  <div id="banner" class="banner"></div>
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
        <div class="split" style="margin-top:12px"><label class="muted"><input id="executeNow" type="checkbox" checked> 提交后立即执行</label><button class="btn ok" type="submit">提交到当前项目</button></div>
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
</main></div>
<script>
const state={project:null,taskId:null,auto:true,timer:null};
const e=(v)=>String(v||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const t=(v)=>v?v.replace('T',' ').slice(0,19):'-';
const c=(s)=>`status-${String(s||'').replaceAll(' ','_')}`;
async function getj(url){const r=await fetch(url);const d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');return d}
async function postj(url,p){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p||{})});const d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');return d}
function flash(msg,type='info'){const b=document.getElementById('banner');if(!msg){b.className='banner';b.textContent='';return}b.className=`banner show ${type}`;b.textContent=msg}
function metric(label,val){return `<div class="metric"><div class="muted">${e(label)}</div><b>${e(val)}</b></div>`}
function projectCard(p){const s=p.stats||{};return `<div class="card ${p.name===state.project?'active':''}" data-project="${e(p.name)}"><div class="split"><strong>${e(p.name)}</strong><span class="tag ${s.in_progress?'status-in_progress':'status-backlog'}">${s.total||0}</span></div><div class="meta"><span class="tag status-in_progress">进行中 ${s.in_progress||0}</span><span class="tag status-backlog">待办 ${s.backlog||0}</span><span class="tag status-failed">失败 ${((s.failed||0)+(s.cancelled||0))}</span><span class="tag status-done">完成 ${s.done||0}</span></div><div class="body">${e(p.active_summary||'当前没有运行中的任务')}</div></div>`}
function taskCard(x){const body=x.runtime||x.error_message||x.latest||'暂无详细信息';return `<article class="task ${state.taskId===x.id?'sel':''}" data-task="${x.id}"><div class="split"><div><strong>#${x.id} ${e(x.title)}</strong><div class="meta"><span class="tag ${c(x.status)}">${e(x.status)}</span><span class="tag status-backlog">${e(x.priority)}</span><span class="tag status-in_progress">${e(x.agent||'-')}</span><span class="tag status-backlog">重试 ${x.retry_count||0}/${x.max_retries||0}</span>${x.source==='auto-inspect'?'<span class="tag status-failed" title="由 codepilot inspect 自动建议，建议人工确认后再执行">巡检建议</span>':''}</div><div class="meta"><span>阶段: ${e(x.phase||'-')}</span><span>开始: ${e(t(x.started_at))}</span><span>完成: ${e(t(x.completed_at))}</span></div></div></div><div class="body">${e(body)}</div><div class="split" style="margin-top:10px">${x.actions.promote?`<button class="btn secondary" data-action="promote" data-task-id="${x.id}">插队到 P0</button>`:''}${x.actions.retry?`<button class="btn warn" data-action="retry" data-task-id="${x.id}">重试</button>`:''}${x.actions.stop?`<button class="btn danger" data-action="stop" data-task-id="${x.id}">停止</button>`:''}</div></article>`}
function renderList(id,items,empty){document.getElementById(id).innerHTML=items.length?items.map(taskCard).join(''):`<div class="empty">${e(empty)}</div>`}
function bindTaskClicks(){document.querySelectorAll('[data-task]').forEach(n=>n.onclick=(ev)=>{if(ev.target.closest('[data-action]'))return;state.taskId=Number(n.dataset.task);loadTaskDetail()})}
function bindActions(){document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async(ev)=>{ev.preventDefault();ev.stopPropagation();try{const out=await postj(`/api/tasks/${b.dataset.taskId}/${b.dataset.action}`,{});flash(out.message||'操作完成','success');await loadDashboard()}catch(err){flash(err.message,'error')}})}
function bindProjects(){document.querySelectorAll('[data-project]').forEach(n=>n.onclick=()=>{state.project=n.dataset.project;state.taskId=null;loadDashboard()})}
function syncMode(){const m=document.getElementById('mode').value;document.getElementById('plannerWrap').style.display=m==='requirement'?'flex':'none';document.getElementById('executeNow').disabled=m==='task';if(m==='task')document.getElementById('executeNow').checked=false}
function pickTask(tasks){return (tasks.find(x=>x.status==='in_progress')||tasks.find(x=>x.status==='failed')||tasks[0]||{}).id||null}
async function loadTaskDetail(){const box=document.getElementById('taskDetail');if(!state.taskId){box.innerHTML='<div class="empty">先选择一个任务。</div>';return}try{const x=await getj(`/api/tasks/${state.taskId}`);const deps=x.depends_on&&x.depends_on.length?x.depends_on.map(id=>`<span class="tag status-backlog">#${id}</span>`).join(' '):'<span class="muted">无依赖</span>';const logs=x.logs&&x.logs.length?`<div class="detail"><div class="muted">阶段日志摘要</div><pre>${e(x.logs.map(i=>`[${i.phase||'-'}] agent=${i.agent||'-'} exit=${i.exit_code==null?'-':i.exit_code}\n${i.output_excerpt||''}`).join('\n\n'))}</pre></div>`:'';const fail=x.error_message?`<div class="detail"><div class="muted">失败原因</div><pre>${e(x.error_message)}</pre></div>`:'';const delivery=x.delivery_record?`<div class="detail"><div class="muted">交付记录</div><pre>${e(x.delivery_record)}</pre></div>`:'';box.innerHTML=`<div class="detail"><div class="split"><div><strong>#${x.id} ${e(x.title)}</strong><div class="meta"><span class="tag ${c(x.status)}">${e(x.status)}</span><span class="tag status-backlog">${e(x.priority)}</span><span class="tag status-in_progress">${e(x.agent||'-')}</span><span class="tag status-backlog">重试 ${x.retry_count||0}/${x.max_retries||0}</span></div></div><div class="split">${x.actions.promote?`<button class="btn secondary" data-action="promote" data-task-id="${x.id}">插队到 P0</button>`:''}${x.actions.retry?`<button class="btn warn" data-action="retry" data-task-id="${x.id}">重试</button>`:''}${x.actions.stop?`<button class="btn danger" data-action="stop" data-task-id="${x.id}">停止</button>`:''}</div></div><div class="meta"><span>项目: ${e(x.project)}</span><span>阶段: ${e(x.phase||'-')}</span><span>运行态: ${e(x.runtime||'-')}</span></div><div class="meta"><span>依赖: ${deps}</span></div><div class="meta"><span>项目路径: ${e(x.project_path||'-')}</span></div><div class="meta"><span>日志文件: ${e(x.current_log_path||'-')}</span></div></div>${fail}<div class="detail"><div class="muted">任务内容</div><pre>${e(x.content||'暂无任务内容')}</pre></div><div class="detail"><div class="muted">实时日志 / 最近输出</div><pre>${e(x.log_text||'还没有可显示的日志')}</pre></div>${delivery}${logs}`;bindActions()}catch(err){box.innerHTML=`<div class="empty">${e(err.message)}</div>`}}
async function loadDashboard(){try{const data=await getj(state.project?`/api/projects/${encodeURIComponent(state.project)}`:'/api/projects');state.project=data.selected_project;document.getElementById('projects').innerHTML=(data.projects||[]).length?data.projects.map(projectCard).join(''):'<div class="empty">还没有项目。先执行一次 codepilot init。</div>';bindProjects();document.getElementById('jobs').innerHTML=(data.jobs||[]).length?data.jobs.map(j=>`<div class="job"><div class="split"><strong>#${j.id} ${e(j.title)}</strong><span class="tag ${c(j.status)}">${e(j.status)}</span></div><div class="meta"><span>planner: ${e(j.planner||'-')}</span><span>agent: ${e(j.agent||'auto')}</span><span>阶段: ${e(j.phase||'-')}</span></div><div class="body">${e(j.summary||j.error||'等待中')}</div></div>`).join(''):'<div class="empty">还没有从 Web UI 发起的需求。</div>';document.getElementById('events').innerHTML=(data.events||[]).length?data.events.map(ev=>`<div class="event"><div class="split"><span class="tag ${c(ev.level||'info')}">${e(ev.level||'info')}</span><span class="muted">${e(t(ev.time))}</span></div><div class="body">${e(ev.message)}</div></div>`).join(''):'<div class="empty">最近还没有事件。</div>';const p=(data.projects||[]).find(i=>i.name===data.selected_project);document.getElementById('projectTitle').textContent=p?p.name:'项目总览';document.getElementById('projectPath').textContent=p?p.path:'当前没有已注册项目';const s=p?p.stats:{in_progress:0,backlog:0,failed:0,cancelled:0,done:0,total:0};document.getElementById('metrics').innerHTML=[metric('进行中',s.in_progress||0),metric('待办',s.backlog||0),metric('失败 / 取消',(s.failed||0)+(s.cancelled||0)),metric('已完成',s.done||0),metric('总任务',s.total||0)].join('');const tasks=data.tasks||[];renderList('running',tasks.filter(x=>x.status==='in_progress'),'当前没有运行中的任务');renderList('backlog',tasks.filter(x=>x.status==='backlog'),'当前 backlog 为空');renderList('failed',tasks.filter(x=>x.status==='failed'||x.status==='cancelled'),'当前没有失败或取消的任务');renderList('done',tasks.filter(x=>x.status==='done').slice(0,8),'还没有已完成任务');bindTaskClicks();bindActions();const ids=tasks.map(x=>x.id);if(!state.taskId&&tasks.length)state.taskId=pickTask(tasks);else if(state.taskId&&!ids.includes(state.taskId))state.taskId=pickTask(tasks);await loadTaskDetail()}catch(err){flash(err.message,'error')}}
function schedule(){clearInterval(state.timer);if(!state.auto)return;state.timer=setInterval(loadDashboard,3000)}
document.getElementById('refreshBtn').onclick=loadDashboard;document.getElementById('toggleBtn').onclick=()=>{state.auto=!state.auto;document.getElementById('toggleBtn').textContent=`自动刷新：${state.auto?'开':'关'}`;schedule()};document.getElementById('mode').onchange=syncMode;document.getElementById('composer').onsubmit=async(ev)=>{ev.preventDefault();if(!state.project){flash('当前没有已注册项目，先执行一次 codepilot init。','error');return}const payload={project:state.project,title:document.getElementById('titleInput').value,content:document.getElementById('contentInput').value,priority:document.getElementById('priority').value,agent:document.getElementById('agent').value,planner:document.getElementById('planner').value,execute:document.getElementById('executeNow').checked};try{const mode=document.getElementById('mode').value;let out;if(mode==='task'){out=await postj('/api/tasks',payload);state.taskId=out.task.id;document.getElementById('contentInput').value=''}else{out=await postj('/api/requirements',payload)}document.getElementById('titleInput').value='';flash(out.message||'提交成功','success');await loadDashboard()}catch(err){flash(err.message,'error')}};
document.getElementById('goalSubmit').onclick=async()=>{if(!state.project){flash('当前没有已注册项目，先执行一次 codepilot init。','error');return}const text=document.getElementById('goalText').value.trim();if(!text){flash('输入不能为空。','error');return}if(new Blob([text]).size>4096){flash('输入超过 4KB 限制。','error');return}const cat=document.getElementById('goalCategory').value;document.getElementById('goalSubmit').disabled=true;const ab=document.getElementById('goalAnswer');ab.className='banner';ab.textContent='';try{const out=await postj('/api/goal',{project:state.project,text:text,category:cat});if(out.intent==='question'||out.intent==='command'){ab.className='banner show info';ab.textContent=out.message||'完成';ab.style.whiteSpace='pre-wrap'}else{flash(out.message||'提交成功','success');await loadDashboard()}document.getElementById('goalText').value=''}catch(err){flash(err.message,'error')}finally{document.getElementById('goalSubmit').disabled=false}};document.getElementById('goalText').onkeydown=(ev)=>{if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();document.getElementById('goalSubmit').click()}};
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
        except (RuntimeError, ValueError) as exc:
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
