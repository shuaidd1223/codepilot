"""Session actions for the Web UI."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from codepilot.storage import database as db
from codepilot.webapp.action_session_history import (
    _session_message_payload,
)


def _normalize_text(text: str) -> str:
    return str(text or "").strip()


def _session_title_from_text(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    return normalized[:40] + ("…" if len(normalized) > 40 else "")


_SESSION_RUNS_LOCK = threading.Lock()
_SESSION_RUNS: dict[tuple[int, int], "_ActiveSessionRun"] = {}


@dataclass
class _ActiveSessionRun:
    session_id: int
    assistant_message_id: int
    project: str
    stop_event: threading.Event
    thread: threading.Thread


def _emit_session_run_event(
    *,
    project: str,
    session_id: int,
    assistant_message_id: int,
    event_type: str,
    status: str,
    content_delta: str = "",
    content_snapshot: str = "",
    tool_calls: Optional[list[dict]] = None,
    opencode_session_id: str = "",
    error: str = "",
) -> None:
    from codepilot.core import progress_bus

    message = {
        "started": "会话开始处理",
        "delta": "会话输出更新",
        "tool": "会话调用工具",
        "summary": "会话状态更新",
        "done": "会话回复完成",
        "error": "会话回复失败",
        "cancelled": "会话回复已停止",
    }.get(event_type, "会话状态更新")
    extra = {
        "project": project,
        "session_id": session_id,
        "assistant_message_id": assistant_message_id,
        "status": status,
        "content_delta": content_delta,
        "content_snapshot": content_snapshot,
        "tool_calls": tool_calls or [],
        "opencode_session_id": opencode_session_id,
    }
    if error:
        extra["error"] = error
    progress_bus.emit(
        stage="session-run",
        event_type=event_type,
        level="error" if event_type == "error" else "info",
        message=message,
        extra=extra,
    )


def _update_streaming_message(
    assistant_message_id: int,
    *,
    content: str,
    status: str,
    tool_calls: Optional[list[dict]] = None,
    opencode_session_id: str = "",
    intent: str = "streaming",
    error: str = "",
) -> dict | None:
    metadata = {
        "status": status,
        "tool_calls": tool_calls or [],
        "opencode_session_id": opencode_session_id,
    }
    if error:
        metadata["error"] = error
    return db.update_session_message(
        assistant_message_id,
        content=content,
        intent=intent,
        metadata=metadata,
    )


_AGENT_MODE_TO_OPENCODE_AGENT = {
    "codepilot": "codepilot",
    "build": "build",
    "plan": "plan",
    "review": "codepilot",
    "inspect": "codepilot",
    "task": "codepilot",
}

_AGENT_MODE_LABELS = {
    "codepilot": "CodePilot",
    "build": "Build",
    "plan": "Plan",
    "review": "代码审查",
    "inspect": "项目巡检",
    "task": "创建任务",
}

_AGENT_MODE_PROMPT_PREFIX = {
    "review": (
        "工作类型：代码审查。\n"
        "请只读地分析当前仓库的相关改动或被指定的代码片段；"
        "按验收标准、潜在风险、可维护性、测试覆盖给出结构化审查意见，并标明阻塞项。"
        "不要直接修改或写入文件。"
    ),
    "inspect": (
        "工作类型：项目巡检。\n"
        "请走 CodePilot 巡检工作流：使用只读 MCP 工具汇总当前任务/失败/风险/依赖等信号，"
        "输出可执行的下一步建议，不要修改任何代码。"
    ),
    "task": (
        "工作类型：创建任务。\n"
        "请把用户需求拆为结构化 CodePilot 任务：先给出最小可执行的任务清单和验收点，"
        "再通过 CodePilot MCP 把任务落库（包含标题、内容、优先级、agent）。"
    ),
}


def _normalize_session_runtime_config(runtime_config: Optional[dict]) -> dict:
    runtime = runtime_config if isinstance(runtime_config, dict) else {}
    raw = (
        runtime.get("agentMode")
        or runtime.get("agent_mode")
        or runtime.get("taskMode")
        or runtime.get("task_mode")
        or "codepilot"
    )
    agent_mode = str(raw).strip()
    if agent_mode not in _AGENT_MODE_TO_OPENCODE_AGENT:
        agent_mode = "codepilot"
    return {
        "agent_mode": agent_mode,
        "agent": _AGENT_MODE_TO_OPENCODE_AGENT[agent_mode],
    }


def _session_runtime_prompt(text: str, runtime_config: Optional[dict]) -> str:
    runtime = _normalize_session_runtime_config(runtime_config)
    prefix = _AGENT_MODE_PROMPT_PREFIX.get(runtime["agent_mode"])
    if not prefix:
        return text
    return f"{prefix}\n\n用户输入：\n{text}"


def _session_runtime_agent(runtime_config: Optional[dict]) -> str:
    return _normalize_session_runtime_config(runtime_config)["agent"]


def _register_session_run(active: _ActiveSessionRun) -> None:
    with _SESSION_RUNS_LOCK:
        _SESSION_RUNS[(active.session_id, active.assistant_message_id)] = active


def _unregister_session_run(session_id: int, assistant_message_id: int) -> None:
    with _SESSION_RUNS_LOCK:
        _SESSION_RUNS.pop((session_id, assistant_message_id), None)


def _run_session_message_stream(
    *,
    session_id: int,
    project: str,
    text: str,
    assistant_message_id: int,
    stop_event: threading.Event,
    runtime_config: Optional[dict] = None,
) -> None:
    content_snapshot = ""
    tool_calls: list[dict] = []
    opencode_session_id = ""

    def on_stream_event(event: dict) -> None:
        nonlocal content_snapshot, tool_calls, opencode_session_id
        event_type = str(event.get("type") or "summary")
        status = str(event.get("status") or ("done" if event_type == "done" else "running"))
        content_delta = str(event.get("content_delta") or "")
        if event.get("content_snapshot") is not None:
            content_snapshot = str(event.get("content_snapshot") or "")
        elif content_delta:
            content_snapshot += content_delta
        if isinstance(event.get("tool_calls"), list):
            tool_calls = list(event.get("tool_calls") or [])
        opencode_session_id = str(event.get("opencode_session_id") or opencode_session_id)
        if event_type in {"delta", "tool", "summary", "started"}:
            _update_streaming_message(
                assistant_message_id,
                content=content_snapshot,
                status=status,
                tool_calls=tool_calls,
                opencode_session_id=opencode_session_id,
            )
        _emit_session_run_event(
            project=project,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            event_type=event_type,
            status=status,
            content_delta=content_delta,
            content_snapshot=content_snapshot,
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            error=str(event.get("error") or ""),
        )

    try:
        from codepilot.opencode.session import run_opencode_message_stream

        result = run_opencode_message_stream(
            project,
            _session_runtime_prompt(text, runtime_config),
            source="web",
            external_session_id=str(session_id),
            agent=_session_runtime_agent(runtime_config),
            on_event=on_stream_event,
            stop_event=stop_event,
        )
        ok = bool(result.get("ok"))
        opencode_session_id = str(result.get("opencode_session_id") or opencode_session_id)
        tool_calls = list(result.get("tool_calls") or tool_calls)
        final_intent = "opencode" if ok else str(result.get("intent") or "error")
        final_status = "done" if ok else ("cancelled" if final_intent == "cancelled" else "error")
        final_message = str(result.get("message") or content_snapshot or "")
        if final_status == "cancelled" and content_snapshot and final_message not in content_snapshot:
            final_message = content_snapshot.rstrip() + "\n\n（已停止）"
        _update_streaming_message(
            assistant_message_id,
            content=final_message,
            status=final_status,
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            intent=final_intent,
            error="" if ok else final_message,
        )
        _emit_session_run_event(
            project=project,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            event_type=final_status,
            status=final_status,
            content_snapshot=final_message,
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            error="" if ok else final_message,
        )
    except Exception as exc:
        final_message = f"OpenCode 执行失败：{exc}"
        _update_streaming_message(
            assistant_message_id,
            content=final_message,
            status="error",
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            intent="error",
            error=final_message,
        )
        _emit_session_run_event(
            project=project,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            event_type="error",
            status="error",
            content_snapshot=content_snapshot,
            tool_calls=tool_calls,
            opencode_session_id=opencode_session_id,
            error=final_message,
        )
    finally:
        _unregister_session_run(session_id, assistant_message_id)


def _start_session_message_stream(
    session_id: int,
    project: str,
    text: str,
    assistant_message_id: int,
    *,
    runtime_config: Optional[dict] = None,
) -> None:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_session_message_stream,
        kwargs={
            "session_id": session_id,
            "project": project,
            "text": text,
            "assistant_message_id": assistant_message_id,
            "stop_event": stop_event,
            "runtime_config": runtime_config,
        },
        daemon=True,
        name=f"codepilot-session-{session_id}-{assistant_message_id}",
    )
    _register_session_run(
        _ActiveSessionRun(
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            project=project,
            stop_event=stop_event,
            thread=thread,
        )
    )
    _emit_session_run_event(
        project=project,
        session_id=session_id,
        assistant_message_id=assistant_message_id,
        event_type="started",
        status="running",
    )
    thread.start()


def send_session_message_action(
    session_id: int,
    text: str,
    *,
    run_async: bool = False,
    runtime_config: Optional[dict] = None,
) -> dict:
    """Send a Web UI session message through OpenCode."""
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    project = session["project"]
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")
    text = _normalize_text(text)
    if not text:
        raise RuntimeError("输入不能为空。")
    existing_messages = db.list_session_messages(session_id)
    if not existing_messages:
        db.update_session(session_id, title=_session_title_from_text(text))
    user_message = db.create_session_message(
        session_id, "user", text,
        metadata={"workflow_phase": "intake"},
    )
    if run_async:
        assistant_message = db.create_session_message(
            session_id,
            "assistant",
            "",
            intent="streaming",
            metadata={
                "status": "running",
                "tool_calls": [],
                "opencode_session_id": "",
            },
        )
        assistant_message_id = int(assistant_message["id"])
        normalized_runtime = _normalize_session_runtime_config(runtime_config)
        assistant_message = db.update_session_message(
            assistant_message_id,
            metadata={
                "status": "running",
                "tool_calls": [],
                "opencode_session_id": "",
                "runtime": normalized_runtime,
            },
        ) or assistant_message
        _start_session_message_stream(
            session_id,
            project,
            text,
            assistant_message_id,
            runtime_config=normalized_runtime,
        )
        return {
            "ok": True,
            "intent": "opencode",
            "status": "running",
            "message": "",
            "task_ids": [],
            "user_message_id": int(user_message["id"]),
            "assistant_message_id": assistant_message_id,
            "user_message": _session_message_payload(user_message),
            "assistant_message": _session_message_payload(assistant_message),
        }

    from codepilot.opencode.session import run_opencode_message

    result = run_opencode_message(
        project,
        _session_runtime_prompt(text, runtime_config),
        source="web",
        external_session_id=str(session_id),
        agent=_session_runtime_agent(runtime_config),
    )
    intent = "opencode" if result.get("ok") else "error"
    reply = str(result.get("message") or "")
    db.create_session_message(
        session_id,
        "assistant",
        reply,
        intent=intent,
        metadata={
            "opencode_session_id": result.get("opencode_session_id") or "",
            "tool_calls": result.get("tool_calls") or [],
        },
    )
    return {
        "ok": bool(result.get("ok")),
        "intent": intent,
        "message": reply,
        "task_ids": [],
        "opencode_session_id": result.get("opencode_session_id") or "",
        "tool_calls": result.get("tool_calls") or [],
    }


def stop_session_run_action(session_id: int, message_id: int) -> dict:
    db.init_db()
    session = db.get_session(session_id)
    if not session:
        raise RuntimeError(f"会话 #{session_id} 不存在。")
    key = (int(session_id), int(message_id))
    with _SESSION_RUNS_LOCK:
        active = _SESSION_RUNS.get(key)
    if not active:
        raise RuntimeError("当前会话没有可停止的运行。")
    active.stop_event.set()
    _update_streaming_message(
        int(message_id),
        content="正在停止当前回复…",
        status="cancelled",
        intent="cancelled",
    )
    _emit_session_run_event(
        project=active.project,
        session_id=int(session_id),
        assistant_message_id=int(message_id),
        event_type="cancelled",
        status="cancelled",
        content_snapshot="正在停止当前回复…",
    )
    return {"ok": True, "message": "已请求停止当前回复。", "assistant_message_id": int(message_id)}


def update_project_permission_action(project: str, mode: str = "") -> dict:
    """读取或更新项目的 AGENTS.toml [opencode.permission] mode。

    * 若 ``mode`` 为空字符串，只读取当前值并返回。
    * 若 ``mode`` 为 ``ask`` / ``full_access`` / ``custom``，更新配置并写回文件。

    每次读写都会通过 ``_canonical_config`` + ``render_agents_toml`` 标准化整个
    AGENTS.toml，保证格式一致性。
    """
    import tomllib

    from codepilot.commands.config_cmd import _canonical_config, render_agents_toml
    from codepilot.core.config import find_config

    db.init_db()
    project_info = db.get_project(project)
    if not project_info:
        raise RuntimeError(f"项目 '{project}' 不存在。")

    project_path = Path(project_info["path"]).resolve()
    config_path = find_config(project_path) or project_path / "AGENTS.toml"
    if not config_path or not config_path.is_file():
        raise RuntimeError(f"项目 '{project}' 没有 AGENTS.toml 配置文件。")

    raw_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        raw_data = {}
    opencode = raw_data.get("opencode") if isinstance(raw_data.get("opencode"), dict) else {}
    permission = opencode.get("permission") if isinstance(opencode.get("permission"), dict) else {}
    current_mode = str(permission.get("mode") or "ask").strip()

    if mode:
        normalized = mode.strip().lower().replace(" ", "_")
        valid_modes = {"ask", "full_access", "custom"}
        if normalized not in valid_modes:
            raise RuntimeError(
                f"无效的权限模式: '{mode}'。仅支持: ask, full_access, custom。"
            )
        if normalized != current_mode:
            if "opencode" not in raw_data or not isinstance(raw_data["opencode"], dict):
                raw_data["opencode"] = {}
            if "permission" not in raw_data["opencode"] or not isinstance(
                raw_data["opencode"]["permission"], dict
            ):
                raw_data["opencode"]["permission"] = {}
            raw_data["opencode"]["permission"]["mode"] = normalized
            canonical = _canonical_config(raw_data, project_name=project_path.name)
            content = render_agents_toml(canonical)
            config_path.write_text(content, encoding="utf-8")
            current_mode = normalized

    return {"ok": True, "mode": current_mode}
