"""Shared inspect-to-workflow helpers.

This module is the thin core used by CLI, Web UI, MCP, chat, and Feishu. It
keeps inspect findings as reviewable workflow context before anything is
materialized into backlog tasks.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from codepilot.core.workflow_state import (
    advance_or_update_agent_phase,
    complete_workflow,
    read_workflow_state,
    start_workflow,
    update_workflow_state,
    workflow_dirs,
)
from codepilot.storage import database as db


_PROMOTE_PREFIX = "promote_inspect_report_"


def _now_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_candidate_token(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip())
    text = text.strip("-_.")
    return text[:80] or "candidate"


def _candidate_seed_file(raw_path: Any, *, project_path: Path | None = None) -> str:
    text = str(raw_path or "").strip().replace("\\", "/")
    if not text:
        return ""
    text = re.sub(r":\d+(?::\d+)?$", "", text).strip()
    if project_path is not None:
        root = project_path.resolve().as_posix().rstrip("/")
        if text.lower().startswith((root + "/").lower()):
            text = text[len(root) + 1 :]
    text = re.sub(r"^[a-zA-Z]:/+", "", text).lstrip("/")
    parts = [part for part in text.split("/") if part and part not in {".", ".."}]
    return "/".join(parts[-8:])


def _candidate_id_seed(item: dict[str, Any], *, reason: str = "", project_path: Path | None = None) -> str:
    files = sorted(
        normalized
        for path in item.get("files") or []
        if (normalized := _candidate_seed_file(path, project_path=project_path))
    )
    payload = {
        "source": sorted(str(source or "").strip() for source in item.get("signal_keys") or [] if str(source).strip()),
        "title": " ".join(str(item.get("title") or "").strip().lower().split()),
        "files": files,
        "reason": str(reason or item.get("reason") or "actionable").strip().lower(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_candidate_id(item: dict[str, Any], *, reason: str = "", project_path: Path | None = None) -> str:
    """Return a stable inspect candidate id and update *item* when missing."""
    existing = str(item.get("candidate_id") or "").strip()
    if existing:
        return _safe_candidate_token(existing)
    seed = _candidate_id_seed(item, reason=reason, project_path=project_path)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    candidate_id = f"inspect-{digest}"
    item["candidate_id"] = candidate_id
    return candidate_id


def _clone_candidate(item: Any, *, reason: str = "", project_path: Path | None = None) -> dict[str, Any]:
    candidate = dict(item) if isinstance(item, dict) else {"title": str(item or "")}
    ensure_candidate_id(candidate, reason=reason, project_path=project_path)
    return candidate


def _candidate_title_list(items: list[dict[str, Any]], *, limit: int = 4) -> str:
    titles = [str(item.get("title") or "").strip() for item in items if str(item.get("title") or "").strip()]
    if not titles:
        return "无可执行巡检候选"
    suffix = "" if len(titles) <= limit else f" 等 {len(titles)} 项"
    return "、".join(titles[:limit]) + suffix


def _inspect_next_actions(project_name: str, context_path: Path, context: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    created_preview = context.get("created_preview") if isinstance(context.get("created_preview"), list) else []
    report_only = context.get("report_only") if isinstance(context.get("report_only"), list) else []
    context_arg = _quote_arg(str(context_path))
    project_arg = _quote_arg(project_name)
    if created_preview:
        actions.append(
            {
                "id": "create_inspect_tasks",
                "label": f"创建 {len(created_preview)} 个高置信巡检任务",
                "risk": "medium",
                "suggested_command": f"codepilot workflow next -p {project_arg} --action create_inspect_tasks --json",
                "context_path": str(context_path),
            }
        )
    for item in report_only:
        candidate_id = ensure_candidate_id(item, reason=str(item.get("reason") or "report_only"))
        actions.append(
            {
                "id": f"{_PROMOTE_PREFIX}{candidate_id}",
                "label": f"提升报告项：{str(item.get('title') or candidate_id)}",
                "risk": "medium",
                "suggested_command": (
                    f"codepilot workflow next -p {project_arg} "
                    f"--action {_PROMOTE_PREFIX}{candidate_id} --json"
                ),
                "candidate_id": candidate_id,
                "context_path": str(context_path),
            }
        )
    actions.append(
        {
            "id": "plan_from_inspect",
            "label": "基于巡检上下文生成可审查计划",
            "risk": "low",
            "suggested_command": f"codepilot plan -p {project_arg} \"巡检建议\" --json  # source {context_arg}",
            "context_path": str(context_path),
        }
    )
    return actions


def _quote_arg(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return '""'
    if re.search(r"\s", text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _candidate_reason(item: Any, default: str) -> str:
    if isinstance(item, dict):
        return str(item.get("reason") or default)
    return default


def _normalize_inspect_result(result: dict[str, Any], *, project_path: Path) -> dict[str, Any]:
    created_preview = [
        _clone_candidate(item, reason="actionable", project_path=project_path) for item in result.get("created") or []
    ]
    report_only = [
        _clone_candidate(item, reason=_candidate_reason(item, "report_only"), project_path=project_path)
        for item in result.get("report_only") or []
    ]
    dropped = [
        _clone_candidate(item, reason=_candidate_reason(item, "dropped"), project_path=project_path)
        for item in result.get("dropped") or []
    ]
    skipped = [
        _clone_candidate(item, reason=_candidate_reason(item, "skipped"), project_path=project_path)
        for item in result.get("skipped") or []
    ]
    return {
        "quality_summary": dict(result.get("quality_summary") or {}),
        "created_preview": created_preview,
        "report_only": report_only,
        "dropped": dropped,
        "skipped": skipped,
    }


def write_inspect_workflow_context(
    project_info: dict[str, Any],
    result: dict[str, Any],
    *,
    source_command: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Persist one dry-run inspect result as project-local workflow context."""
    project_name = str(project_info.get("name") or result.get("project") or "").strip()
    project_path = Path(str(project_info.get("path") or "")).resolve()
    if not project_name:
        raise ValueError("project name is required")
    if not project_path.is_dir():
        raise ValueError(f"project path not found: {project_path}")

    session = session_id or f"inspect-{_now_slug()}"
    dirs = workflow_dirs(project_path)
    context_path = dirs["context"] / f"{session}.json"
    normalized = _normalize_inspect_result(result, project_path=project_path)
    state = start_workflow(
        project_path,
        mode="inspect",
        session_id=session,
        current_phase="inspection_ready",
        context_path=context_path,
        artifact_paths={"context": context_path},
    )
    context: dict[str, Any] = {
        "artifact_type": "inspect",
        "project": project_name,
        "project_path": str(project_path),
        "summary": _candidate_title_list(normalized["created_preview"] + normalized["report_only"]),
        "quality_summary": normalized["quality_summary"],
        "created_preview": normalized["created_preview"],
        "report_only": normalized["report_only"],
        "dropped": normalized["dropped"],
        "skipped": normalized["skipped"],
        "context_path": str(context_path),
        "source_command": source_command,
        "state": state,
    }
    context["next_actions"] = _inspect_next_actions(project_name, context_path, context)
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    complete_workflow(project_path, "inspect")
    final_state = update_workflow_state(project_path, "inspect", next_actions=context["next_actions"])
    advance_or_update_agent_phase(
        project_path,
        "explore",
        goal=f"巡检项目 {project_name} 并生成安全下一步",
        artifact_paths={"context": context_path, "inspect_context": context_path},
        next_actions=context["next_actions"],
        next_action_details=context["next_actions"],
        mode_state=final_state,
    )
    return {
        "project": project_name,
        "context_path": str(context_path),
        "next_actions": context["next_actions"],
        "quality_summary": context["quality_summary"],
        "created_preview_count": len(context["created_preview"]),
        "report_only_count": len(context["report_only"]),
    }


def _resolve_context_path(project_path: Path, raw_path: str | Path | None) -> Path:
    if raw_path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = project_path / candidate
        resolved = candidate.resolve()
    else:
        state = read_workflow_state(project_path, mode="inspect")
        if not state:
            raise click.ClickException("没有可用的 inspect workflow context。")
        resolved = Path(str(state.get("context_path") or "")).resolve()
    if not resolved.is_relative_to(project_path):
        raise click.ClickException(f"inspect context 必须位于项目目录内：{resolved}")
    if not resolved.is_file():
        raise click.ClickException(f"inspect context 不存在：{resolved}")
    return resolved


def read_inspect_workflow_context(project_info: dict[str, Any], context_path: str | Path | None = None) -> dict[str, Any] | None:
    project_path = Path(str(project_info.get("path") or "")).resolve()
    try:
        path = _resolve_context_path(project_path, context_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, click.ClickException):
        return None
    if not isinstance(payload, dict) or payload.get("artifact_type") != "inspect":
        return None
    payload["context_path"] = str(path)
    return payload


def _task_priority(item: dict[str, Any], default_priority: str) -> str:
    priority = str(item.get("priority") or default_priority or "P3").upper()
    return "P3" if priority == "P4" else priority


def _materialize_candidates(
    project_info: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    priority: str = "P3",
    agent: str = "codex",
) -> dict[str, Any]:
    from codepilot.commands.inspect import _materialize_inspection_output

    project_name = str(project_info["name"])
    project_path = Path(str(project_info["path"])).resolve()
    normalized: list[dict[str, Any]] = []
    for item in candidates:
        candidate = dict(item)
        candidate["priority"] = _task_priority(candidate, priority)
        normalized.append(candidate)
    created, skipped = _materialize_inspection_output(
        normalized,
        max_new_tasks=len(normalized),
        project_name=project_name,
        project_path=project_path,
        priority=priority,
        agent=agent,
        dry_run=False,
    )
    return {
        "created_count": len(created),
        "skipped_count": len(skipped),
        "created": created,
        "skipped": skipped,
    }


def execute_inspect_workflow_action(
    project_info: dict[str, Any],
    *,
    action_id: str,
    context_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute an allowlisted inspect workflow action."""
    project_path = Path(str(project_info.get("path") or "")).resolve()
    path = _resolve_context_path(project_path, context_path)
    context = read_inspect_workflow_context(project_info, path)
    if not context:
        raise click.ClickException("inspect workflow context 不可读。")
    action = str(action_id or "").strip()
    if action == "create_inspect_tasks":
        return _materialize_candidates(project_info, list(context.get("created_preview") or []))
    if action.startswith(_PROMOTE_PREFIX):
        candidate_id = action[len(_PROMOTE_PREFIX) :]
        for item in context.get("report_only") or []:
            if str(item.get("candidate_id") or "") == candidate_id:
                return _materialize_candidates(project_info, [dict(item)])
        raise click.ClickException(f"未找到 inspect report candidate：{candidate_id}")
    if action == "plan_from_inspect":
        from codepilot.commands.plan import write_plan_artifact

        requirement = (
            f"根据 CodePilot 巡检上下文推进：{context.get('summary') or '巡检建议'}。"
            "先生成可审查计划，不导入 backlog。"
        )
        return write_plan_artifact(
            project_info,
            requirement,
            source="inspect",
            source_path=str(path),
            use_wiki=True,
        )
    raise click.ClickException(f"不支持的 inspect workflow action：{action}")


def run_inspect_preview_to_workflow(
    project_info: dict[str, Any],
    *,
    max_new: int | None = None,
    planner: str | None = None,
    agent: str = "codex",
) -> dict[str, Any]:
    """Run inspect in dry-run mode and persist the result as workflow context."""
    from codepilot.commands.inspect import load_project_config, resolve_planner, run_inspection

    cfg = load_project_config(project_info)
    ins = cfg.inspect
    limit = max_new if max_new is not None else ins.max_new_tasks_per_round
    effective_planner = resolve_planner(cfg, "inspect", explicit=planner)
    result = run_inspection(
        {"name": project_info["name"], "path": project_info["path"], "config_file": project_info.get("config_file")},
        max_new_tasks=limit,
        signals=ins.signals,
        auto_execute=False,
        priority=ins.priority,
        agent=agent,
        planner=effective_planner,
        dry_run=True,
    )
    context = write_inspect_workflow_context(
        project_info,
        result,
        source_command=f"codepilot inspect -p {project_info['name']} --once --dry-run --write-workflow --json",
    )
    return {"ok": not bool(result.get("error")), "inspect": result, "workflow_context": context}
