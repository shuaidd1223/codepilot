"""Project-local event sink registry and delivery helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REGISTRY_RELATIVE_PATH = Path(".codepilot") / "events" / "sinks.json"
DEFAULT_JSONL_RELATIVE_PATH = Path(".codepilot") / "events" / "events.jsonl"
SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
SAFE_EVENT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,119}$")
EVENT_SCHEMAS = (
    {
        "type": "test.event",
        "description": "事件 sink 连通性测试事件。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["message"],
    },
    {
        "type": "doctor.checked",
        "description": "doctor 环境检查或 doctor --fix 后的健康检查事件。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["ok", "checks", "fix"],
    },
    {
        "type": "task.updated",
        "description": "任务状态、阶段或执行结果发生变化。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["task_id", "status", "phase", "summary"],
    },
    {
        "type": "workflow.changed",
        "description": "clarify/plan/go 等工作流状态发生变化。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["mode", "phase", "state_path"],
    },
    {
        "type": "agent.prompt.submitted",
        "description": "任意智能体运行时收到用户提示或上游任务输入。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["provider", "agent_runtime", "prompt"],
    },
    {
        "type": "agent.tool.started",
        "description": "任意智能体运行时开始执行工具调用。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["provider", "agent_runtime", "tool", "args"],
    },
    {
        "type": "agent.tool.finished",
        "description": "任意智能体运行时完成工具调用。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["provider", "agent_runtime", "tool", "exit_code", "status"],
    },
    {
        "type": "agent.run.stopped",
        "description": "任意智能体运行时正常停止。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["provider", "agent_runtime", "reason"],
    },
    {
        "type": "agent.run.failed",
        "description": "任意智能体运行时失败停止。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["provider", "agent_runtime", "error"],
    },
    {
        "type": "exec.started",
        "description": "项目内 exec 烟测或命令执行开始。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["provider", "agent_runtime", "command", "cwd"],
    },
    {
        "type": "exec.completed",
        "description": "项目内 exec 命令执行成功完成。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["provider", "agent_runtime", "command", "cwd", "exit_code", "duration_ms"],
    },
    {
        "type": "exec.failed",
        "description": "项目内 exec 命令执行失败或超时。",
        "required_fields": ["schema_version", "id", "type", "source", "project", "timestamp", "payload"],
        "payload_fields": ["provider", "agent_runtime", "command", "cwd", "exit_code", "duration_ms", "error"],
    },
)


class EventPluginError(ValueError):
    """Raised when an event sink registry operation is invalid."""


def default_registry() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "sinks": [
            {
                "name": "local-jsonl",
                "type": "jsonl",
                "enabled": False,
                "path": DEFAULT_JSONL_RELATIVE_PATH.as_posix(),
                "events": ["*"],
            }
        ],
    }


def event_schemas() -> list[dict[str, Any]]:
    return [dict(item) for item in EVENT_SCHEMAS]


def registry_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / REGISTRY_RELATIVE_PATH


def _validate_name(name: str) -> str:
    value = str(name or "").strip()
    if not SAFE_NAME_RE.match(value):
        raise EventPluginError("事件 sink 名称只能包含字母、数字、点、下划线或短横线，且不能超过 64 字符。")
    return value


def _validate_event_type(event_type: str) -> str:
    value = str(event_type or "").strip()
    if value == "*":
        return value
    if not SAFE_EVENT_RE.match(value):
        raise EventPluginError("事件类型只能包含字母、数字、点、下划线、冒号或短横线。")
    return value


def _resolve_project_relative_path(project_root: str | Path, raw_path: str | Path) -> tuple[Path, str]:
    root = Path(project_root).expanduser().resolve()
    text = str(raw_path or "").strip()
    if not text:
        raise EventPluginError("事件 sink 路径不能为空。")
    candidate = Path(text)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise EventPluginError("事件 sink 路径必须位于项目目录内。") from exc
    if relative.parts and relative.parts[0] in {"..", ""}:
        raise EventPluginError("事件 sink 路径必须位于项目目录内。")
    return resolved, relative.as_posix()


def _normalize_sink(project_root: str | Path, sink: dict[str, Any]) -> dict[str, Any]:
    name = _validate_name(str(sink.get("name") or ""))
    sink_type = str(sink.get("type") or "").strip().lower()
    if sink_type != "jsonl":
        raise EventPluginError(f"暂不支持事件 sink 类型：{sink_type or '-'}")
    _, relative_path = _resolve_project_relative_path(project_root, str(sink.get("path") or ""))
    events = sink.get("events")
    if not isinstance(events, list) or not events:
        events = ["*"]
    return {
        "name": name,
        "type": sink_type,
        "enabled": bool(sink.get("enabled", True)),
        "path": relative_path,
        "events": [_validate_event_type(str(item)) for item in events],
    }


def ensure_event_registry(project_root: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    path = registry_path(project_root)
    if path.is_file():
        registry = load_event_registry(project_root)
        return {"path": str(path), "status": "exists", "registry": registry}
    registry = default_registry()
    if dry_run:
        return {"path": str(path), "status": "would_create", "registry": registry}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "status": "created", "registry": registry}


def load_event_registry(project_root: str | Path) -> dict[str, Any]:
    path = registry_path(project_root)
    if not path.is_file():
        return default_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise EventPluginError(f"事件 sink registry 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise EventPluginError("事件 sink registry 顶层结构必须是 JSON object。")
    sinks = data.get("sinks")
    if not isinstance(sinks, list):
        sinks = []
    normalized = []
    for item in sinks:
        if not isinstance(item, dict):
            continue
        normalized.append(_normalize_sink(project_root, item))
    return {"schema_version": SCHEMA_VERSION, "sinks": normalized}


def save_event_registry(project_root: str | Path, registry: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "sinks": [_normalize_sink(project_root, item) for item in registry.get("sinks", []) if isinstance(item, dict)],
    }
    path = registry_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


def register_jsonl_sink(
    project_root: str | Path,
    *,
    name: str,
    path: str,
    events: list[str] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    ensure_event_registry(project_root)
    registry = load_event_registry(project_root)
    sink = _normalize_sink(
        project_root,
        {
            "name": name,
            "type": "jsonl",
            "enabled": enabled,
            "path": path,
            "events": events or ["*"],
        },
    )
    sinks = [item for item in registry["sinks"] if item["name"] != sink["name"]]
    sinks.append(sink)
    registry["sinks"] = sorted(sinks, key=lambda item: item["name"])
    save_event_registry(project_root, registry)
    return sink


def set_sink_enabled(project_root: str | Path, name: str, enabled: bool) -> dict[str, Any]:
    registry = load_event_registry(project_root)
    target_name = _validate_name(name)
    updated: dict[str, Any] | None = None
    for sink in registry["sinks"]:
        if sink["name"] != target_name:
            continue
        sink["enabled"] = bool(enabled)
        updated = dict(sink)
        break
    if updated is None:
        raise EventPluginError(f"未找到事件 sink：{target_name}")
    save_event_registry(project_root, registry)
    return updated


def _event_matches(sink: dict[str, Any], event_type: str) -> bool:
    patterns = sink.get("events")
    if not isinstance(patterns, list) or not patterns:
        return True
    return "*" in patterns or event_type in {str(item) for item in patterns}


def build_test_event(project_name: str, event_type: str) -> dict[str, Any]:
    return build_event(
        project_name,
        event_type,
        source="codepilot.event.test",
        payload={"message": "CodePilot event sink test"},
        event_id_prefix="test",
    )


def build_event(
    project_name: str,
    event_type: str,
    *,
    source: str,
    payload: dict[str, Any],
    event_id_prefix: str = "evt",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": f"{event_id_prefix}-{now.strftime('%Y%m%d%H%M%S')}",
        "type": _validate_event_type(event_type),
        "source": str(source or "codepilot"),
        "project": str(project_name or ""),
        "timestamp": now.isoformat(),
        "payload": dict(payload or {}),
    }


def dispatch_event_to_sinks(project_root: str | Path, event: dict[str, Any]) -> list[dict[str, Any]]:
    registry = load_event_registry(project_root)
    event_type = _validate_event_type(str(event.get("type") or "test.event"))
    results: list[dict[str, Any]] = []
    for sink in registry["sinks"]:
        result = {"name": sink["name"], "type": sink["type"], "status": "skipped", "path": sink["path"]}
        if not sink.get("enabled", True):
            result["status"] = "disabled"
            results.append(result)
            continue
        if not _event_matches(sink, event_type):
            result["status"] = "filtered"
            results.append(result)
            continue
        target, _ = _resolve_project_relative_path(project_root, sink["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        result["status"] = "delivered"
        results.append(result)
    return results
