"""Project-local hook wrapper planning helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codepilot.core import event_plugins


SCHEMA_VERSION = 1
REGISTRY_RELATIVE_PATH = Path(".codepilot") / "hooks" / "registry.json"
WRAPPER_RELATIVE_PATH = Path(".codepilot") / "hooks" / "codepilot-hook-wrapper"
HOOK_TARGETS = ("agent.prompt.submitted", "agent.tool.started", "agent.tool.finished", "agent.run.stopped", "agent.run.failed")
SUPPORTED_PROVIDERS = ("codex", "claude", "opencode", "gemini", "custom")
HOOK_LOG_RELATIVE_PATH = Path(".codepilot") / "hooks" / "logs" / "hook-events.jsonl"


class HookRegistryError(ValueError):
    """Raised when hook registry operations are unsafe or invalid."""


def registry_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / REGISTRY_RELATIVE_PATH


def codex_hooks_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / ".codex" / "hooks.json"


def default_registry(*, enabled: bool = True) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "hooks": [
            {
                "name": "codepilot-event-wrapper",
                "type": "codex-wrapper",
                "enabled": bool(enabled),
                "targets": list(HOOK_TARGETS),
                "wrapper": WRAPPER_RELATIVE_PATH.as_posix(),
                "install_policy": "project-registry-only",
                "providers": list(SUPPORTED_PROVIDERS),
            }
        ],
    }


def load_registry(project_root: str | Path) -> dict[str, Any]:
    path = registry_path(project_root)
    if not path.is_file():
        return default_registry(enabled=False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HookRegistryError(f"hook registry 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise HookRegistryError("hook registry 顶层结构必须是 JSON object。")
    hooks = data.get("hooks")
    if not isinstance(hooks, list):
        hooks = []
    normalized = []
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        normalized.append(
            {
                "name": str(hook.get("name") or "codepilot-event-wrapper"),
                "type": str(hook.get("type") or "codex-wrapper"),
                "enabled": bool(hook.get("enabled", False)),
                "targets": [str(item) for item in hook.get("targets", HOOK_TARGETS) if str(item).strip()],
                "wrapper": str(hook.get("wrapper") or WRAPPER_RELATIVE_PATH.as_posix()),
                "install_policy": str(hook.get("install_policy") or "project-registry-only"),
                "providers": [
                    str(item).lower()
                    for item in hook.get("providers", SUPPORTED_PROVIDERS)
                    if str(item).lower() in SUPPORTED_PROVIDERS
                ]
                or list(SUPPORTED_PROVIDERS),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "hooks": normalized}


def write_registry(project_root: str | Path, registry: dict[str, Any]) -> dict[str, Any]:
    path = registry_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "hooks": registry.get("hooks") if isinstance(registry.get("hooks"), list) else [],
    }
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_registry(project_root)


def plan_hooks(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    hooks_path = codex_hooks_path(root)
    status = "preserve_existing" if hooks_path.exists() else "missing"
    return {
        "registry_path": str(registry_path(root)),
        "registry": default_registry(enabled=True),
        "codex_hooks": {
            "path": str(hooks_path),
            "status": status,
            "policy": "never_modify_in_stage_2",
        },
    }


def validate_hooks(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    registry = load_registry(root)
    errors: list[str] = []
    for hook in registry.get("hooks", []):
        if not hook.get("name"):
            errors.append("hook name 不能为空。")
        unknown_targets = [item for item in hook.get("targets", []) if item not in HOOK_TARGETS]
        if unknown_targets:
            errors.append(f"未知 lifecycle event：{', '.join(unknown_targets)}")
        unknown_providers = [item for item in hook.get("providers", []) if item not in SUPPORTED_PROVIDERS]
        if unknown_providers:
            errors.append(f"未知 provider：{', '.join(unknown_providers)}")
    hooks_path = codex_hooks_path(root)
    return {
        "valid": not errors,
        "errors": errors,
        "registry_path": str(registry_path(root)),
        "registry": registry,
        "providers": list(SUPPORTED_PROVIDERS),
        "lifecycle_events": list(HOOK_TARGETS),
        "codex_hooks": {
            "path": str(hooks_path),
            "status": "preserve_existing" if hooks_path.exists() else "missing",
            "policy": "project_only_no_global_writes",
        },
    }


def hook_log_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / HOOK_LOG_RELATIVE_PATH


def append_hook_log(project_root: str | Path, event: dict[str, Any]) -> Path:
    path = hook_log_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return path


def read_hook_logs(project_root: str | Path, *, limit: int = 50) -> dict[str, Any]:
    path = hook_log_path(project_root)
    if not path.is_file():
        return {"path": str(path), "events": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-max(1, int(limit or 1)) :]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return {"path": str(path), "events": events}


def build_hook_test_event(project_name: str, event_type: str, *, provider: str) -> dict[str, Any]:
    normalized_provider = str(provider or "custom").lower()
    if normalized_provider not in SUPPORTED_PROVIDERS:
        raise HookRegistryError(f"不支持的 provider：{provider}")
    if event_type not in HOOK_TARGETS:
        raise HookRegistryError(f"不支持的 hook event：{event_type}")
    return event_plugins.build_event(
        project_name,
        event_type,
        source="codepilot.hook.test",
        payload={
            "provider": normalized_provider,
            "agent_runtime": normalized_provider,
            "message": "CodePilot hook lifecycle test",
        },
        event_id_prefix="hook",
    )


def install_plan(project_root: str | Path, *, dry_run: bool) -> dict[str, Any]:
    if not dry_run:
        raise HookRegistryError("hook install 当前只允许 --dry-run；真实 .codex/hooks.json 安装尚未开放。")
    plan = plan_hooks(project_root)
    return {
        **plan,
        "dry_run": True,
        "actions": [
            {
                "kind": "registry",
                "path": plan["registry_path"],
                "status": "would_write",
                "detail": "将写入项目级 hook registry；不会修改 .codex/hooks.json。",
            }
        ],
    }


def uninstall_plan(project_root: str | Path, *, dry_run: bool) -> dict[str, Any]:
    if not dry_run:
        raise HookRegistryError("hook uninstall 当前只允许 --dry-run；真实 .codex/hooks.json 卸载尚未开放。")
    registry = load_registry(project_root)
    return {
        "registry_path": str(registry_path(project_root)),
        "registry": registry,
        "dry_run": True,
        "codex_hooks": {
            "path": str(codex_hooks_path(project_root)),
            "status": "not_modified",
            "policy": "preserve_user_hooks",
        },
        "actions": [
            {
                "kind": "registry",
                "path": str(registry_path(project_root)),
                "status": "would_disable",
                "detail": "将停用项目级 hook registry 条目；不会修改 .codex/hooks.json。",
            }
        ],
    }
