"""Project-local skill catalog helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
CATALOG_RELATIVE_PATH = Path(".codepilot") / "skills" / "catalog.json"
SAFE_SKILL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
SUPPORTED_PROVIDERS = ("codex", "claude", "gemini", "custom")


class SkillCatalogError(ValueError):
    """Raised when a skill catalog operation is invalid."""


def default_skills() -> list[dict[str, Any]]:
    return [
        {
            "name": "deep-interview",
            "kind": "builtin",
            "enabled": False,
            "description": "分层澄清技能：把模糊需求拆成目标、边界、验收标准和待确认问题。",
            "tags": ["workflow", "clarify", "interview"],
            "commands": ["clarify"],
            "supported_providers": list(SUPPORTED_PROVIDERS),
            "entrypoint_command": "clarify",
            "requires_enabled": True,
        },
        {
            "name": "ralplan",
            "kind": "builtin",
            "enabled": False,
            "description": "规划技能：把已澄清需求转成可执行任务、验收标准和验证命令。",
            "tags": ["workflow", "plan"],
            "commands": ["plan"],
            "supported_providers": list(SUPPORTED_PROVIDERS),
            "entrypoint_command": "plan",
            "requires_enabled": True,
        },
        {
            "name": "ralph",
            "kind": "builtin",
            "enabled": False,
            "description": "执行技能：按任务模板驱动 builder/reviewer 闭环执行。",
            "tags": ["workflow", "run", "review"],
            "commands": ["run", "go"],
            "supported_providers": list(SUPPORTED_PROVIDERS),
            "entrypoint_command": "go",
            "requires_enabled": True,
        },
        {
            "name": "build-fix",
            "kind": "builtin",
            "enabled": False,
            "description": "质量闭环技能：收集失败任务、触发重试修复并运行验证命令。",
            "tags": ["quality", "retry", "verification"],
            "commands": ["build-fix"],
            "supported_providers": list(SUPPORTED_PROVIDERS),
            "entrypoint_command": "build-fix",
            "requires_enabled": True,
        },
        {
            "name": "wiki",
            "kind": "builtin",
            "enabled": False,
            "description": "项目记忆技能：查询或显式沉淀当前项目 wiki 内容。",
            "tags": ["memory", "wiki", "context"],
            "commands": ["wiki"],
            "supported_providers": list(SUPPORTED_PROVIDERS),
            "entrypoint_command": "wiki",
            "requires_enabled": True,
        },
    ]


def default_catalog() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "skills": default_skills()}


def catalog_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / CATALOG_RELATIVE_PATH


def _validate_name(name: str) -> str:
    value = str(name or "").strip()
    if not SAFE_SKILL_RE.match(value):
        raise SkillCatalogError("技能名称只能包含字母、数字、点、下划线或短横线，且不能超过 64 字符。")
    return value


def _normalize_skill(skill: dict[str, Any]) -> dict[str, Any]:
    name = _validate_name(str(skill.get("name") or ""))
    tags = skill.get("tags") if isinstance(skill.get("tags"), list) else []
    commands = skill.get("commands") if isinstance(skill.get("commands"), list) else []
    providers = skill.get("supported_providers") if isinstance(skill.get("supported_providers"), list) else []
    normalized_providers = [str(item).lower() for item in providers if str(item).lower() in SUPPORTED_PROVIDERS]
    return {
        "name": name,
        "kind": str(skill.get("kind") or "project").strip() or "project",
        "enabled": bool(skill.get("enabled", False)),
        "description": str(skill.get("description") or "").strip(),
        "tags": [str(item).strip() for item in tags if str(item).strip()],
        "commands": [str(item).strip() for item in commands if str(item).strip()],
        "supported_providers": normalized_providers or list(SUPPORTED_PROVIDERS),
        "entrypoint_command": str(skill.get("entrypoint_command") or (commands[0] if commands else "")).strip(),
        "requires_enabled": bool(skill.get("requires_enabled", True)),
    }


def load_skill_catalog(project_root: str | Path) -> dict[str, Any]:
    path = catalog_path(project_root)
    if not path.is_file():
        return default_catalog()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SkillCatalogError(f"技能 catalog 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise SkillCatalogError("技能 catalog 顶层结构必须是 JSON object。")
    skills = data.get("skills")
    normalized = [_normalize_skill(item) for item in skills if isinstance(item, dict)] if isinstance(skills, list) else []
    by_name = {item["name"]: item for item in default_skills()}
    by_name.update({item["name"]: item for item in normalized})
    return {"schema_version": SCHEMA_VERSION, "skills": sorted(by_name.values(), key=lambda item: item["name"])}


def save_skill_catalog(project_root: str | Path, catalog: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "skills": sorted(
            [_normalize_skill(item) for item in catalog.get("skills", []) if isinstance(item, dict)],
            key=lambda item: item["name"],
        ),
    }
    path = catalog_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


def ensure_skill_catalog(project_root: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    path = catalog_path(project_root)
    if path.is_file():
        return {"path": str(path), "status": "exists", "catalog": load_skill_catalog(project_root)}
    catalog = default_catalog()
    if dry_run:
        return {"path": str(path), "status": "would_create", "catalog": catalog}
    save_skill_catalog(project_root, catalog)
    return {"path": str(path), "status": "created", "catalog": catalog}


def list_skills(project_root: str | Path) -> list[dict[str, Any]]:
    return load_skill_catalog(project_root)["skills"]


def search_skills(project_root: str | Path, query: str) -> list[dict[str, Any]]:
    needle = str(query or "").strip().lower()
    if not needle:
        return list_skills(project_root)
    results = []
    for skill in list_skills(project_root):
        haystack = " ".join(
            [
                skill["name"],
                skill.get("description") or "",
                " ".join(skill.get("tags") or []),
                " ".join(skill.get("commands") or []),
            ]
        ).lower()
        if needle in haystack:
            results.append(skill)
    return results


def get_skill(project_root: str | Path, name: str) -> dict[str, Any]:
    target = _validate_name(name)
    for skill in list_skills(project_root):
        if skill["name"] == target:
            return skill
    raise SkillCatalogError(f"未找到技能：{target}")


def set_skill_enabled(project_root: str | Path, name: str, enabled: bool) -> dict[str, Any]:
    catalog = load_skill_catalog(project_root)
    target = _validate_name(name)
    updated = None
    for skill in catalog["skills"]:
        if skill["name"] == target:
            skill["enabled"] = bool(enabled)
            updated = dict(skill)
            break
    if updated is None:
        raise SkillCatalogError(f"未找到技能：{target}")
    save_skill_catalog(project_root, catalog)
    return updated
