"""Headless OpenCode session adapter for external channels."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from codepilot.ai_support.cli_families import env_var_for
from codepilot.core.config import load_project_config
from codepilot.mcp.launchers import build_mcp_launch_plan
from codepilot.opencode.env import build_agent_launch_env, build_opencode_config_from_agents_config, clean_agent_env
from codepilot.opencode.model_state import resolve_project_model_selection, sync_latest_project_model_selection
from codepilot.opencode.paths import opencode_runtime_config_path, opencode_runtime_db_path
from codepilot.storage import database as db


OPENCODE_CHAT_SERVICE = "opencode_chat"
DEFAULT_OPENCODE_AGENT = "codepilot"
DEFAULT_OPENCODE_MESSAGE_TIMEOUT_SECONDS = 300.0
StreamCallback = Callable[[dict[str, Any]], None]


def run_opencode_message(
    project: str,
    text: str,
    *,
    source: str,
    external_session_id: str,
    timeout_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Send one message through OpenCode and persist the external session mapping."""
    db.init_db()
    project_name = str(project or "").strip()
    message = str(text or "").strip()
    source_name = str(source or "").strip() or "external"
    external_id = str(external_session_id or "").strip() or "default"
    if not project_name:
        return _error("缺少项目名称，无法启动 OpenCode 会话。")
    if not message:
        return _error("输入不能为空。")

    project_info = db.get_project(project_name)
    if not project_info:
        return _error(f"项目 '{project_name}' 未注册。")
    project_name = str(project_info["name"])

    cwd = Path(str(project_info["path"])).resolve()
    scope = _scope(source_name, external_id, project_name)
    previous = db.get_service_state(OPENCODE_CHAT_SERVICE, scope) or {}
    previous_meta = previous.get("meta") if isinstance(previous.get("meta"), dict) else {}
    previous_session_id = str(previous_meta.get("opencode_session_id") or "").strip()
    timeout = _resolve_timeout_seconds(timeout_seconds)

    try:
        launch = _prepare_headless_launch(
            project_name=project_name,
            project_path=cwd,
            message=message,
            previous_session_id=previous_session_id,
        )
        completed = subprocess.run(
            launch["command"],
            cwd=str(cwd),
            env=launch["env"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        detail = _trim_text(_subprocess_output_text(exc.stderr or exc.output), 800)
        suffix = f"：{detail}" if detail else "。"
        return _error(f"OpenCode 执行超时（{_format_seconds(timeout)} 秒）{suffix}")
    except Exception as exc:
        return _error(f"OpenCode 启动失败：{exc}")

    parsed = _parse_json_events(completed.stdout or "")
    if completed.returncode != 0:
        detail = _trim_text(completed.stderr or completed.stdout or "没有错误输出。", 800)
        return _error(f"OpenCode 执行失败（退出码 {completed.returncode}）：{detail}")
    _sync_project_model_after_headless(project_name, cwd)

    # 只有在有有效 session_id 时才更新服务状态
    session_id = str(parsed["session_id"] or previous_session_id or "").strip()
    if session_id:
        db.upsert_service_state(
            OPENCODE_CHAT_SERVICE,
            scope,
            pid=0,
            status="active",
            log_path="",
            meta={
                "source": source_name,
                "external_session_id": external_id,
                "project": project_name,
                "opencode_session_id": session_id,
                "last_message_at": _now_iso(),
            },
        )

    # 检查是否有有效回复，如果没有则返回错误
    if not parsed["message"] and not parsed["tool_calls"]:
        return _error("OpenCode 执行完成但未返回有效回复，可能输出格式异常。")

    reply = parsed["message"] or _trim_text(completed.stdout or "", 1200) or "OpenCode 已完成处理，但没有返回可展示文本。"
    return {
        "ok": True,
        "intent": "opencode",
        "project": project_name,
        "source": source_name,
        "external_session_id": external_id,
        "opencode_session_id": session_id,
        "message": reply,
        "tool_calls": parsed["tool_calls"],
    }


def run_opencode_message_stream(
    project: str,
    text: str,
    *,
    source: str,
    external_session_id: str,
    timeout_seconds: int | float | None = None,
    on_event: StreamCallback | None = None,
    stop_event: Any = None,
) -> dict[str, Any]:
    """Send one OpenCode message and report JSON-line output incrementally."""
    db.init_db()
    project_name = str(project or "").strip()
    message = str(text or "").strip()
    source_name = str(source or "").strip() or "external"
    external_id = str(external_session_id or "").strip() or "default"
    if not project_name:
        return _error("缺少项目名称，无法启动 OpenCode 会话。")
    if not message:
        return _error("输入不能为空。")

    project_info = db.get_project(project_name)
    if not project_info:
        return _error(f"项目 '{project_name}' 未注册。")
    project_name = str(project_info["name"])

    cwd = Path(str(project_info["path"])).resolve()
    scope = _scope(source_name, external_id, project_name)
    previous = db.get_service_state(OPENCODE_CHAT_SERVICE, scope) or {}
    previous_meta = previous.get("meta") if isinstance(previous.get("meta"), dict) else {}
    previous_session_id = str(previous_meta.get("opencode_session_id") or "").strip()
    timeout = _resolve_timeout_seconds(timeout_seconds)

    try:
        launch = _prepare_headless_launch(
            project_name=project_name,
            project_path=cwd,
            message=message,
            previous_session_id=previous_session_id,
        )
        proc = subprocess.Popen(
            launch["command"],
            cwd=str(cwd),
            env=launch["env"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return _error(f"OpenCode 启动失败：{exc}")

    session_id = previous_session_id
    assistant_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    _emit_stream_event(
        on_event,
        "started",
        project=project_name,
        session_id=session_id,
        content_snapshot="",
        tool_calls=tool_calls,
    )
    line_queue: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        stdout = getattr(proc, "stdout", None)
        try:
            while stdout:
                raw = stdout.readline()
                if not raw:
                    break
                line_queue.put(raw)
        finally:
            line_queue.put(None)

    reader = threading.Thread(target=_reader, daemon=True, name="codepilot-opencode-stream")
    reader.start()

    try:
        while True:
            if _stop_requested(stop_event):
                _terminate_process(proc)
                result = _cancelled("OpenCode 会话已停止。")
                _emit_stream_event(
                    on_event,
                    "cancelled",
                    project=project_name,
                    session_id=session_id,
                    content_snapshot="".join(assistant_parts),
                    tool_calls=tool_calls,
                )
                return result
            if time.monotonic() > deadline:
                _terminate_process(proc)
                detail = _trim_text(_read_stderr(proc), 800)
                suffix = f"：{detail}" if detail else "。"
                result = _error(f"OpenCode 执行超时（{_format_seconds(timeout)} 秒）{suffix}")
                _emit_stream_event(
                    on_event,
                    "error",
                    project=project_name,
                    session_id=session_id,
                    content_snapshot="".join(assistant_parts),
                    tool_calls=tool_calls,
                    error=result["message"],
                )
                return result

            try:
                raw_line = line_queue.get(timeout=0.05)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue

            if raw_line is None:
                if proc.poll() is not None:
                    break
                continue
            if raw_line:
                parsed_event = _loads_json_line(raw_line)
                if parsed_event is None:
                    continue
                next_session_id = _find_session_id(parsed_event)
                if next_session_id:
                    session_id = next_session_id
                text_delta = _find_assistant_text(parsed_event)
                if text_delta:
                    assistant_parts.append(text_delta)
                    _emit_stream_event(
                        on_event,
                        "delta",
                        project=project_name,
                        session_id=session_id,
                        content_delta=text_delta,
                        content_snapshot="".join(assistant_parts),
                        tool_calls=tool_calls,
                    )
                tool_name = _find_tool_name(parsed_event)
                if tool_name:
                    tool_calls.append({"name": tool_name})
                    _emit_stream_event(
                        on_event,
                        "tool",
                        project=project_name,
                        session_id=session_id,
                        content_snapshot="".join(assistant_parts),
                        tool_calls=tool_calls,
                    )
                continue

        return_code = proc.wait(timeout=1)
    except Exception as exc:
        _terminate_process(proc)
        result = _error(f"OpenCode 执行失败：{exc}")
        _emit_stream_event(
            on_event,
            "error",
            project=project_name,
            session_id=session_id,
            content_snapshot="".join(assistant_parts),
            tool_calls=tool_calls,
            error=result["message"],
        )
        return result

    if return_code != 0:
        detail = _trim_text(_read_stderr(proc), 800)
        result = _error(f"OpenCode 执行失败（退出码 {return_code}）：{detail or '没有错误输出。'}")
        _emit_stream_event(
            on_event,
            "error",
            project=project_name,
            session_id=session_id,
            content_snapshot="".join(assistant_parts),
            tool_calls=tool_calls,
            error=result["message"],
        )
        return result

    _sync_project_model_after_headless(project_name, cwd)
    if session_id:
        db.upsert_service_state(
            OPENCODE_CHAT_SERVICE,
            scope,
            pid=0,
            status="active",
            log_path="",
            meta={
                "source": source_name,
                "external_session_id": external_id,
                "project": project_name,
                "opencode_session_id": session_id,
                "last_message_at": _now_iso(),
            },
        )

    reply = _trim_text("".join(assistant_parts).strip(), 4000)
    if not reply and not tool_calls:
        result = _error("OpenCode 执行完成但未返回有效回复，可能输出格式异常。")
        _emit_stream_event(
            on_event,
            "error",
            project=project_name,
            session_id=session_id,
            content_snapshot=reply,
            tool_calls=tool_calls,
            error=result["message"],
        )
        return result

    result = {
        "ok": True,
        "intent": "opencode",
        "project": project_name,
        "source": source_name,
        "external_session_id": external_id,
        "opencode_session_id": session_id,
        "message": reply or "OpenCode 已完成处理，但没有返回可展示文本。",
        "tool_calls": tool_calls,
    }
    _emit_stream_event(
        on_event,
        "done",
        project=project_name,
        session_id=session_id,
        content_snapshot=result["message"],
        tool_calls=tool_calls,
    )
    return result


def _prepare_headless_launch(
    *,
    project_name: str,
    project_path: Path,
    message: str,
    previous_session_id: str,
) -> dict[str, Any]:
    cfg = load_project_config(project_path)
    executable = _resolve_opencode_executable(cfg)
    source_root = Path(__file__).resolve().parents[2]
    plan = build_mcp_launch_plan(
        "opencode",
        executable=executable,
        prompt="",
        mcp_servers={
            "codepilot": {
                "command": sys.executable,
                "args": ["-m", "codepilot", "mcp", "serve", "--transport", "stdio", "--project", project_name],
                "env": {"PYTHONPATH": _pythonpath_with_source_root(source_root)},
            }
        },
        config_path=opencode_runtime_config_path(project_name),
        opencode_config=build_opencode_config_from_agents_config(
            cfg,
            preferred_model=resolve_project_model_selection(
                project_name,
                project_path,
                db_path=opencode_runtime_db_path(project_name),
            ),
        ),
    )
    _write_launch_config_files(plan.config_files, cwd=project_path)
    env = os.environ.copy()
    env.update(plan.env)
    configured_keys = build_agent_launch_env("opencode", cfg)
    env.update(configured_keys)
    clean_agent_env(env, cfg)
    default_agent = str((plan.mcp_config or {}).get("default_agent") or DEFAULT_OPENCODE_AGENT).strip()
    command = [executable, "run", "--agent", default_agent or DEFAULT_OPENCODE_AGENT, "--format", "json"]
    if previous_session_id:
        command.extend(["--session", previous_session_id])
    else:
        command.extend(["--title", _session_title(message)])
    command.append(message)
    return {"command": command, "env": env}


def _sync_project_model_after_headless(project_name: str, project_path: Path) -> None:
    try:
        sync_latest_project_model_selection(
            project_name,
            project_path,
            db_path=opencode_runtime_db_path(project_name),
        )
    except Exception:
        return


def _resolve_opencode_executable(cfg: Any) -> str:
    env_var = env_var_for("opencode")
    if env_var:
        value = os.environ.get(env_var, "").strip()
        if value:
            return shutil.which(value) or value
    configured = str((getattr(cfg, "commands", {}) or {}).get("opencode", "") or "").strip()
    if configured:
        return shutil.which(configured) or configured
    return shutil.which("opencode") or "opencode"


def _write_launch_config_files(config_files: dict[str, str], *, cwd: Path) -> None:
    for raw_path, content in config_files.items():
        path = Path(raw_path)
        if not path.is_absolute():
            path = cwd / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _pythonpath_with_source_root(source_root: Path) -> str:
    existing = os.environ.get("PYTHONPATH", "").strip()
    if not existing:
        return str(source_root)
    entries = [str(source_root), *[item for item in existing.split(os.pathsep) if item]]
    return os.pathsep.join(dict.fromkeys(entries))


def _parse_json_events(stdout: str) -> dict[str, Any]:
    session_id = ""
    assistant_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = _find_session_id(event) or session_id
        text = _find_assistant_text(event)
        if text:
            assistant_parts.append(text)
        tool_name = _find_tool_name(event)
        if tool_name:
            tool_calls.append({"name": tool_name})
    return {
        "session_id": session_id,
        "message": _trim_text("".join(assistant_parts).strip(), 4000),
        "tool_calls": tool_calls,
    }


def _loads_json_line(raw_line: str) -> dict[str, Any] | None:
    line = str(raw_line or "").strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _emit_stream_event(
    callback: StreamCallback | None,
    event_type: str,
    *,
    project: str,
    session_id: str = "",
    content_delta: str = "",
    content_snapshot: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    error: str = "",
) -> None:
    if not callback:
        return
    payload = {
        "type": event_type,
        "project": project,
        "opencode_session_id": session_id,
        "status": _stream_status_for_type(event_type),
        "content_delta": content_delta,
        "content_snapshot": content_snapshot,
        "tool_calls": list(tool_calls or []),
    }
    if error:
        payload["error"] = error
    try:
        callback(payload)
    except Exception:
        return


def _stream_status_for_type(event_type: str) -> str:
    if event_type == "done":
        return "done"
    if event_type == "error":
        return "error"
    if event_type == "cancelled":
        return "cancelled"
    return "running"


def _stop_requested(stop_event: Any) -> bool:
    if not stop_event:
        return False
    try:
        return bool(stop_event.is_set())
    except Exception:
        return False


def _terminate_process(proc: Any) -> None:
    try:
        proc.terminate()
    except Exception:
        return
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _read_stderr(proc: Any) -> str:
    stderr = getattr(proc, "stderr", None)
    if not stderr:
        return ""
    try:
        return _subprocess_output_text(stderr.read())
    except Exception:
        return ""


def _find_session_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("sessionID", "sessionId", "session_id", "session"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            if isinstance(raw, dict):
                nested = _find_session_id(raw)
                if nested:
                    return nested
        for child in value.values():
            nested = _find_session_id(child)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_session_id(item)
            if nested:
                return nested
    return ""


def _find_assistant_text(event: dict[str, Any]) -> str:
    if str(event.get("role") or "").lower() == "assistant":
        text = event.get("text") or event.get("content")
        if isinstance(text, str):
            return text
    message = event.get("message")
    if isinstance(message, dict) and str(message.get("role") or "").lower() == "assistant":
        content = message.get("content") or message.get("text")
        if isinstance(content, str):
            return content
    part = event.get("part")
    if isinstance(part, dict) and str(part.get("type") or "").lower() in {"text", "message"}:
        text = part.get("text") or part.get("content")
        if isinstance(text, str):
            return text
    text = event.get("text")
    if isinstance(text, str) and "assistant" in str(event.get("type") or "").lower():
        return text
    return ""


def _find_tool_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("tool", "toolName", "tool_name", "name"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip() and "tool" in str(value.get("type") or key).lower():
                return raw.strip()
        for child in value.values():
            nested = _find_tool_name(child)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_tool_name(item)
            if nested:
                return nested
    return ""


def _scope(source: str, external_session_id: str, project: str) -> str:
    return f"{source}:{external_session_id}:{project}"


def _session_title(text: str) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:40] or "CodePilot 会话"


def _trim_text(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _resolve_timeout_seconds(value: int | float | None) -> float:
    raw: object = value
    if raw is None:
        raw = os.environ.get("CODEPILOT_OPENCODE_TIMEOUT_SECONDS", "")
    try:
        seconds = float(raw) if raw not in {"", None} else DEFAULT_OPENCODE_MESSAGE_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        seconds = DEFAULT_OPENCODE_MESSAGE_TIMEOUT_SECONDS
    return max(1.0, seconds)


def _format_seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")


def _subprocess_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _error(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "intent": "error",
        "message": str(message or "OpenCode 会话失败。").strip(),
        "task_ids": [],
    }


def _cancelled(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "intent": "cancelled",
        "message": str(message or "OpenCode 会话已停止。").strip(),
        "task_ids": [],
    }
