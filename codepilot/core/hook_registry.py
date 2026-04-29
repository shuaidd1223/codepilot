"""Project-local hook wrapper planning helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REGISTRY_RELATIVE_PATH = Path(".codepilot") / "hooks" / "registry.json"
WRAPPER_RELATIVE_PATH = Path(".codepilot") / "hooks" / "codepilot-hook-wrapper"
HOOK_TARGETS = ("pre_tool_use", "post_tool_use")


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
