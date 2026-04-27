"""Periodic codebase inspection: surface improvement candidates and enqueue them."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

import click

from codepilot.storage import database as db
from codepilot.ai_support.service import (
    API_PROVIDERS,
    _run_api_provider,
    _run_claude_schema_prompt,
    _run_codex_schema_prompt,
    build_task_markdown_from_plan,
    normalize_agent_name,
)
from codepilot.commands.add import _resolve_project_strict
from codepilot.commands import inspect_lifecycle, inspect_service, inspect_signals
from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.config import load_project_config, resolve_planner
from codepilot.core.output import echo
from codepilot.core.paths import global_storage_root
from codepilot.core.runtime import is_process_alive, stop_process_tree
from codepilot.core.task_template import missing_task_template_sections

INSPECT_STATE_DIR = global_storage_root() / "inspect"
SKIPPED_SIGNAL = "（跳过）"


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _service_log_path(project: str) -> Path:
    return inspect_service.service_log_path(project, state_dir=INSPECT_STATE_DIR)


def inspect_service_status(project: str) -> dict:
    return inspect_service.inspect_service_status(
        project,
        state_dir=INSPECT_STATE_DIR,
        get_service_state=db.get_service_state,
        is_alive=is_process_alive,
    )


def _cleanup_inspect_files(project: str) -> None:
    inspect_service.cleanup_inspect_files(
        project,
        clear_service_state=db.clear_service_state,
    )


def _write_inspect_meta(project: str, pid: int, *, interval: int, planner: str, agent: str) -> None:
    inspect_service.write_inspect_meta(
        project,
        pid,
        interval=interval,
        planner=planner,
        agent=agent,
        state_dir=INSPECT_STATE_DIR,
        upsert_service_state=db.upsert_service_state,
        now_iso_fn=_now_iso,
    )


def _spawn_detached_inspect(
    project: str,
    *,
    max_new: int | None,
    dry_run: bool,
    agent: str,
    planner: str | None,
    interval: int | None,
) -> object:
    return inspect_service.spawn_detached_inspect(
        project,
        max_new=max_new,
        dry_run=dry_run,
        agent=agent,
        planner=planner,
        interval=interval,
        state_dir=INSPECT_STATE_DIR,
        now_iso_fn=_now_iso,
    )


def start_inspect_service(
    project: str,
    *,
    max_new: int | None = None,
    dry_run: bool = False,
    agent: str = "codex",
    planner: str | None = None,
    interval: int | None = None,
) -> dict:
    if not project:
        raise RuntimeError("启动巡检必须指定项目。")
    existing = inspect_service_status(project)
    if existing["running"]:
        existing["started"] = False
        return existing
    _cleanup_inspect_files(project)
    proc = _spawn_detached_inspect(
        project,
        max_new=max_new,
        dry_run=dry_run,
        agent=agent,
        planner=planner,
        interval=interval,
    )
    time.sleep(0.8)
    if proc.poll() is not None:
        tail = ""
        try:
            tail = _service_log_path(project).read_text(encoding="utf-8", errors="replace")[-1500:]
        except Exception:
            pass
        raise RuntimeError(f"巡检启动后立即退出（exit={proc.returncode}）\n{tail}")
    proj = db.get_project(project)
    cfg = load_project_config(proj) if proj else None
    effective_interval = interval if interval is not None else int(getattr(getattr(cfg, "inspect", None), "interval_seconds", 1800) or 1800)
    effective_planner = planner or (resolve_planner(cfg, "inspect") if cfg else "codex")
    _write_inspect_meta(project, proc.pid, interval=effective_interval, planner=effective_planner, agent=agent)
    return {"running": True, "started": True, "pid": proc.pid, "project": project, "log": str(_service_log_path(project))}


def stop_inspect_service(project: str) -> dict:
    if not project:
        raise RuntimeError("停止巡检必须指定项目。")
    status = inspect_service_status(project)
    if not status["running"]:
        _cleanup_inspect_files(project)
        return {"stopped": False, "pids": []}
    pid = int(status["pid"])
    db.upsert_service_state(
        "inspect",
        project,
        pid=pid,
        status="stopping",
        log_path=str(_service_log_path(project)),
        heartbeat_at=_now_iso(),
        meta={"project": project, "pid": pid, "stop_requested_at": _now_iso()},
    )
    stop_process_tree(pid, wait_seconds=5)
    if is_process_alive(pid):
        raise RuntimeError(f"无法停止巡检 PID={pid}")
    _cleanup_inspect_files(project)
    return {"stopped": True, "pids": [pid]}

INSPECT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
                    "rationale": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["refactor", "bug", "test", "docs", "perf", "chore"],
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Must quote or cite the specific signal line/file/commit that justifies this candidate. No evidence => candidate is hallucinated and will be dropped.",
                    },
                    "effort": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                        "description": "Rough implementation effort. 'small' = hours, 'medium' = day, 'large' = multi-day.",
                    },
                },
                # OpenAI strict structured-output: every object needs
                # additionalProperties=false AND ALL properties in `required`.
                "required": ["title", "goal", "priority", "rationale", "kind", "evidence", "effort"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

INSPECT_PROMPT = """[Role]
You are the senior inspection engineer for project `{project_name}`.
Look at the signals below and surface 0 to {max_tasks} improvement candidates that are actually worth working on.

[Core principle — ZERO IS PREFERRED OVER FILLER]
- Returning an empty `candidates` array is a valid, encouraged outcome.
- Do NOT invent work to justify your existence. Only raise something if a signal *directly* points to it.
- If every signal is empty / skipped / benign, return `{{"candidates": []}}` — this is the correct answer, not a failure.

[Signal semantics]
- `最近 git 提交`: recent commits. Look for reverts, WIPs, incomplete chains, suspiciously large commits.
- `最近失败或取消的任务`: failed tasks. Recurring error patterns imply a systemic fix.
- `代码里的 TODO/FIXME/XXX`: literal markers with file:line. Only propose cleanup if the marker content describes real work, not placeholders.
- `ruff lint 报告`: style/quality warnings. Prefer grouping by rule code or file.
- `pytest --collect-only 摘要`: collection errors or missing tests. Real collection errors are high priority; low test count is usually NOT actionable alone.
- `依赖健康线索`: missing/stale lockfiles or unpinned deps. Only if signal lists concrete paths.
- `代码规模与复杂度线索`: hotspots. Only if signal already flagged a specific file/function.

[Hard rules]
- Each candidate MUST cite `evidence` referencing a concrete line/file/commit from the signals. No evidence → drop the candidate yourself; do not emit it.
- `title` ≤ 40 Chinese characters; `goal` = 2–3 sentences covering "what to do + why".
- Do NOT propose generic items like "补一下文档", "加日志", "通用优化", "重构一下" unless the signal names the exact target.
- Do NOT repeat anything in the existing-tasks list below.
- Fill `effort` honestly; prefer `small`/`medium`. If it smells like `large`, split or skip.

[Language rules]
- Instruction language is English (above).
- The following output fields MUST be Chinese: `title`, `goal`, `rationale`.
- The following output fields are fixed vocabulary (English): `priority` ∈ P1–P4, `kind` ∈ enum, `effort` ∈ enum.
- `evidence` may be Chinese or English, but must point at a signal line.

## 已存在任务（backlog / in-progress）
{existing_titles}

{signal_sections}

[Output]
Return strict JSON in this shape:
{{"candidates": [{{"title": "...", "goal": "...", "priority": "P3", "rationale": "...", "kind": "refactor", "evidence": "signal 3: codepilot/foo.py:42 TODO ...", "effort": "small"}}]}}
If nothing is worth surfacing, return: {{"candidates": []}}
"""


collect_git_log = inspect_signals.collect_git_log
collect_failed_tasks = inspect_signals.collect_failed_tasks
collect_todos = inspect_signals.collect_todos
collect_ruff = inspect_signals.collect_ruff
collect_pytest_collect = inspect_signals.collect_pytest_collect
collect_dependency_health = inspect_signals.collect_dependency_health
collect_code_metrics = inspect_signals.collect_code_metrics

InspectSignalSpec = inspect_signals.InspectSignalSpec
InspectSignalResult = inspect_signals.InspectSignalResult


INSPECT_SIGNAL_SPECS: tuple[InspectSignalSpec, ...] = (
    InspectSignalSpec(
        key="git_log",
        title="最近 git 提交",
        order=1,
        aliases=("git_log",),
        collector_key="collect_git_log",
        collector_scope="project_path",
    ),
    InspectSignalSpec(
        key="failed_tasks",
        title="最近失败或取消的任务",
        order=2,
        aliases=("failed_tasks",),
        collector_key="collect_failed_tasks",
        collector_scope="project_name",
    ),
    InspectSignalSpec(
        key="todos",
        title="代码里的 TODO/FIXME/XXX",
        order=3,
        aliases=("todos",),
        collector_key="collect_todos",
        collector_scope="project_path",
    ),
    InspectSignalSpec(
        key="ruff",
        title="ruff lint 报告",
        order=4,
        aliases=("ruff",),
        collector_key="collect_ruff",
        collector_scope="project_path",
    ),
    InspectSignalSpec(
        key="pytest",
        title="pytest --collect-only 摘要",
        order=5,
        aliases=("pytest",),
        collector_key="collect_pytest_collect",
        collector_scope="project_path",
    ),
    InspectSignalSpec(
        key="deps",
        title="依赖健康线索",
        order=6,
        aliases=("deps",),
        collector_key="collect_dependency_health",
        collector_scope="project_path",
    ),
    InspectSignalSpec(
        key="code_metrics",
        title="代码规模与复杂度线索",
        order=7,
        aliases=("code_metrics", "code_size", "complexity"),
        collector_key="collect_code_metrics",
        collector_scope="project_path",
    ),
)


def _inspect_signal_collectors() -> dict[str, inspect_signals.InspectSignalCollector]:
    return {
        "collect_git_log": lambda value: collect_git_log(value),
        "collect_failed_tasks": lambda value: collect_failed_tasks(value),
        "collect_todos": lambda value: collect_todos(value),
        "collect_ruff": lambda value: collect_ruff(value),
        "collect_pytest_collect": lambda value: collect_pytest_collect(value),
        "collect_dependency_health": lambda value: collect_dependency_health(value),
        "collect_code_metrics": lambda value: collect_code_metrics(value),
    }


def collect_inspection_signal_results(
    project_name: str,
    project_path: Path,
    *,
    signals: tuple[str, ...],
) -> list[InspectSignalResult]:
    """Aggregate inspect signals with one unified model, order, and combination path."""
    return inspect_signals.collect_signal_results(
        project_name,
        project_path,
        signals=signals,
        specs=INSPECT_SIGNAL_SPECS,
        collectors_by_key=_inspect_signal_collectors(),
        skipped_signal=SKIPPED_SIGNAL,
    )


def _render_signal_sections(signal_results: list[InspectSignalResult]) -> str:
    blocks: list[str] = []
    for signal in sorted(signal_results, key=lambda item: item.order):
        blocks.append(f"## 信号 {signal.order}：{signal.title}\n{signal.content}")
    return "\n\n".join(blocks)


def _existing_titles(project: str) -> str:
    rows = [
        t
        for t in db.list_tasks(project=project)
        if t["status"] in {"backlog", "in_progress"}
    ]
    if not rows:
        return "（无）"
    return "\n".join(f"- {t['title']}" for t in rows[:30])


def _dedup_key(title: str, goal: str) -> str:
    text = (title.strip() + "|" + goal.strip()).lower()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _existing_inspector_titles(project: str) -> set[str]:
    """Return inspector-generated titles that are still unresolved.

    These are exactly the titles that previously created a self-feedback loop:
    inspect saw its own failed tasks in `failed_tasks`, then generated the same
    titles again on later rounds. We deliberately treat any non-done inspector
    task as unresolved and block re-creation until a human retries/resets/edits
    the existing task instead of cloning it.
    """
    return {
        str(t.get("title") or "").strip()
        for t in db.list_tasks(project=project)
        if (t.get("source") or "") == "inspector"
        and t.get("status") in {"backlog", "in_progress", "failed", "cancelled"}
        and str(t.get("title") or "").strip()
    }


def _call_llm(
    prompt: str,
    classifier_provider: str,
    classifier_model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    project_path: str,
    timeout: int,
    planner: str = "claude",
) -> dict:
    # 1) Prefer the configured classifier provider (API with key).
    if classifier_provider and classifier_provider in API_PROVIDERS:
        from dataclasses import replace

        provider = replace(API_PROVIDERS[classifier_provider])
        if classifier_model:
            provider.model = classifier_model
        if api_key:
            provider.api_key = api_key
        if base_url:
            provider.base_url = base_url
        if not provider.requires_api_key() or provider.resolve_api_key():
            raw = _run_api_provider(provider, prompt).strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if "\n" in raw:
                    raw = raw.split("\n", 1)[1]
                if raw.endswith("```"):
                    raw = raw[:-3]
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                raw = raw[start : end + 1]
            return json.loads(raw)

    # 2) Fall back to local CLI — default claude (faster for analysis/inspection).
    normalized = normalize_agent_name(planner) if planner else "claude"
    if normalized in {"claude", "claude-node", "claude-sonnet", "claude-opus", "claude-haiku"}:
        return _run_claude_schema_prompt(
            prompt,
            INSPECT_SCHEMA,
            planner=normalized,
            project_path=project_path,
            timeout=timeout,
        )
    return _run_codex_schema_prompt(
        prompt,
        INSPECT_SCHEMA,
        project_path=project_path,
        timeout=timeout,
    )


def collect_inspection_signals(
    project_name: str,
    project_path: Path,
    *,
    signals: tuple[str, ...],
) -> dict[str, str]:
    """Back-compat wrapper: return the legacy signal map from unified model."""
    return {
        result.key: result.content
        for result in collect_inspection_signal_results(
            project_name,
            project_path,
            signals=signals,
        )
    }


def _build_inspection_prompt(
    *,
    project_name: str,
    max_new_tasks: int,
    signal_results: list[InspectSignalResult],
) -> str:
    existing = _existing_titles(project_name)
    return INSPECT_PROMPT.format(
        project_name=project_name,
        max_tasks=max_new_tasks,
        existing_titles=existing,
        signal_sections=_render_signal_sections(signal_results),
    )


from codepilot.commands.task_quality import (
    INSPECT_FILLER_KEYWORDS as _GENERIC_FILLER_KEYWORDS,
    evidence_grounded_in,
    looks_generic,
)


def _is_empty_signal_content(content: str) -> bool:
    """Return True for signal outputs that carry no actionable findings.

    Empty signals are single-line bracketed status markers like ``（跳过）``,
    ``（无）``, ``（ruff 无发现）`` or whitespace.
    """
    text = (content or "").strip()
    if not text:
        return True
    if "\n" in text:
        return False
    if text.startswith("（") and text.endswith("）"):
        return True
    return False


def _has_substantive_signal(signal_results: list[InspectSignalResult]) -> bool:
    return any(not _is_empty_signal_content(result.content) for result in signal_results)


def _extract_candidates(payload: dict) -> list[dict]:
    candidates = payload.get("candidates") or []
    return candidates if isinstance(candidates, list) else []


def _signal_evidence_tokens(signal_results: list[InspectSignalResult]) -> set[str]:
    """Tokens planner may legitimately cite as evidence (signal titles + content words)."""
    tokens: set[str] = set()
    for result in signal_results:
        if not result.enabled or _is_empty_signal_content(result.content):
            continue
        tokens.add(result.title)
        tokens.add(result.key)
        tokens.add(f"signal {result.order}")
        tokens.add(f"信号 {result.order}")
        for line in result.content.splitlines():
            stripped = line.strip(" -#*`")
            if len(stripped) >= 4:
                tokens.add(stripped)
    return {token for token in tokens if token}


def _candidate_looks_generic(title: str, goal: str) -> bool:
    """Inspect-side filler check: delegates to shared ``looks_generic``."""
    return looks_generic(f"{title} {goal}", _GENERIC_FILLER_KEYWORDS)


def _evidence_references_signal(evidence: str, signal_tokens: set[str]) -> bool:
    """Cheap containment check against known signal tokens (inspect-only concept)."""
    if not signal_tokens:
        return False
    return evidence_grounded_in(evidence, signal_tokens)


def _filter_candidates(
    candidates: list[dict],
    *,
    signal_results: list[InspectSignalResult],
) -> tuple[list[dict], list[dict]]:
    """Drop low-quality / hallucinated candidates before materialization.

    Returns (kept, dropped) where dropped entries carry a ``reason`` string.
    """
    signal_tokens = _signal_evidence_tokens(signal_results)
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            dropped.append({"title": str(item)[:60], "reason": "not_an_object"})
            continue
        title = (item.get("title") or "").strip()
        goal = (item.get("goal") or "").strip()
        evidence = (item.get("evidence") or "").strip()
        if not title or not goal:
            dropped.append({"title": title or "(untitled)", "reason": "missing_title_or_goal"})
            continue
        if len(title) > 40:
            dropped.append({"title": title, "reason": "title_too_long"})
            continue
        if _candidate_looks_generic(title, goal):
            dropped.append({"title": title, "reason": "generic_filler"})
            continue
        if not evidence:
            dropped.append({"title": title, "reason": "missing_evidence"})
            continue
        if not _evidence_references_signal(evidence, signal_tokens):
            dropped.append({"title": title, "reason": "evidence_not_grounded"})
            continue
        kept.append(item)
    return kept, dropped


def _materialize_inspection_output(
    candidates: list[dict],
    *,
    max_new_tasks: int,
    project_name: str,
    project_path: Path,
    priority: str,
    agent: str,
    dry_run: bool,
) -> tuple[list[dict], list[dict]]:
    """Write candidate output to the selected path (preview or DB)."""
    existing_keys = db.existing_dedup_keys(project_name)
    existing_inspector_titles = _existing_inspector_titles(project_name)
    created: list[dict] = []
    skipped: list[dict] = []
    for item in candidates[:max_new_tasks]:
        title = (item.get("title") or "").strip()
        goal = (item.get("goal") or "").strip()
        if not title or not goal:
            continue
        if title in existing_inspector_titles:
            skipped.append({"title": title, "reason": "duplicate_title_history"})
            continue
        key = _dedup_key(title, goal)
        if key in existing_keys:
            skipped.append({"title": title, "reason": "duplicate"})
            continue
        content = _build_content(item, agent=agent, default_priority=priority)
        missing_sections = missing_task_template_sections(content)
        if missing_sections:
            skipped.append(
                {
                    "title": title,
                    "reason": "template_noncompliant",
                    "missing_sections": missing_sections,
                }
            )
            continue
        if dry_run:
            created.append({"title": title, "goal": goal, "priority": item.get("priority") or priority})
            continue
        task = db.create_task(
            project=project_name,
            title=title,
            content=content,
            agent=agent,
            priority=item.get("priority") or priority,
            project_path=str(project_path),
            source="inspector",
            dedup_key=key,
        )
        created.append(task)
        existing_keys.add(key)
        existing_inspector_titles.add(title)
    return created, skipped


def run_inspection(
    project_info: dict,
    *,
    max_new_tasks: int = 3,
    signals: tuple[str, ...] = ("git_log", "failed_tasks", "todos"),
    auto_execute: bool = False,
    priority: str = "P3",
    agent: str = "codex",
    planner: str = "claude",
    dry_run: bool = False,
    timeout: int = 120,
) -> dict:
    project_name = project_info["name"]
    project_path = Path(project_info["path"])

    signal_results = collect_inspection_signal_results(
        project_name,
        project_path,
        signals=signals,
    )

    if not _has_substantive_signal(signal_results):
        return {
            "project": project_name,
            "candidates_total": 0,
            "created": [],
            "skipped": [],
            "dropped": [],
            "auto_execute": auto_execute,
            "reason": "no_substantive_signals",
            "note": "本轮所有信号都是空/跳过，不触发 LLM，避免硬规划填充任务。",
        }

    prompt = _build_inspection_prompt(
        project_name=project_name,
        max_new_tasks=max_new_tasks,
        signal_results=signal_results,
    )

    cfg = load_project_config(project_info)
    classifier_cfg = getattr(cfg, "classifier", None)
    provider_key = classifier_cfg.provider if classifier_cfg else ""
    model = classifier_cfg.model if classifier_cfg else ""
    api_key = cfg.get_provider_api_key(provider_key) if provider_key else None
    provider_cfg = cfg.providers.get(provider_key) if provider_key else None
    base_url = provider_cfg.base_url if provider_cfg else None

    try:
        payload = _call_llm(
            prompt,
            classifier_provider=provider_key,
            classifier_model=model,
            api_key=api_key,
            base_url=base_url,
            project_path=str(project_path),
            timeout=timeout,
            planner=planner,
        )
    except Exception as exc:
        return {
            "project": project_name,
            "error": f"巡检 LLM 调用失败：{exc}",
            "created": [],
        }

    raw_candidates = _extract_candidates(payload)
    kept_candidates, dropped = _filter_candidates(raw_candidates, signal_results=signal_results)
    created, skipped = _materialize_inspection_output(
        kept_candidates,
        max_new_tasks=max_new_tasks,
        project_name=project_name,
        project_path=project_path,
        priority=priority,
        agent=agent,
        dry_run=dry_run,
    )

    return {
        "project": project_name,
        "candidates_total": len(raw_candidates),
        "created": created,
        "skipped": skipped,
        "dropped": dropped,
        "auto_execute": auto_execute,
    }


def _build_content(item: dict, *, agent: str = "codex", default_priority: str = "P3") -> str:
    kind = item.get("kind") or "chore"
    effort = (item.get("effort") or "small").strip() or "small"
    rationale = (item.get("rationale") or "").strip()
    goal = (item.get("goal") or "").strip()
    evidence = (item.get("evidence") or "").strip()
    task_spec = {
        "title": (item.get("title") or "").strip(),
        "agent": agent,
        "priority": (item.get("priority") or "").strip() or default_priority,
        "goal": goal or "根据巡检信号完成针对性处理，并消除对应风险。",
        "acceptance_criteria": [
            "规划证据里的信号已经被实际处理，并能说明对应改动。",
            "改动范围与本任务目标一致，没有扩散到无关模块。",
        ],
        "builder_notes": [
            f"优先按巡检建议执行最小闭环改动（kind={kind}, effort={effort}）。",
            rationale or "结合信号内容确认具体处理路径，再开始实现。",
        ],
        "reviewer_notes": [
            "逐项核对 Planning Evidence 是否被真实引用到改动与验证里。",
            "确认实现没有超出本次巡检建议的目标与边界。",
        ],
        "files": [],
        "notes": [
            f"由 `codepilot inspect` 自动建议（kind={kind}, effort={effort}）。",
            "执行前请人工确认方向与优先级。",
        ] + ([f"补充背景：{rationale}"] if rationale else []),
        "forbidden": [
            "不要因为巡检建议而顺手做无关重构或范围外修补。",
        ],
        "not_in_scope": [
            "与当前巡检信号无直接关系的模块、文档、部署流程。",
        ],
        "evidence": evidence or "（未提供规划依据，建议人工复核）",
        "risk_level": "medium",
        "scope_budget": f"inspect:{effort}",
        "depends_on_indices": [],
    }
    return build_task_markdown_from_plan(task_spec)


def _print_result(result: dict, dry_run: bool) -> None:
    """Print a single inspection result to the terminal."""
    if result.get("error"):
        echo(f"[red]{result['error']}[/red]")
        return

    if result.get("reason") == "no_substantive_signals":
        note = result.get("note") or "所有信号为空，不触发 LLM。"
        echo(f"[dim]{note}[/dim]")
        return

    dropped = result.get("dropped") or []
    echo(
        f"[green]候选总数 {result['candidates_total']}，"
        f"新建 {len(result['created'])}，跳过 {len(result['skipped'])}，"
        f"过滤 {len(dropped)}[/green]"
    )
    for task in result["created"]:
        if dry_run:
            echo(f"  [dim][dry-run][/dim] {task['title']}  [{task.get('priority')}]")
        else:
            echo(f"  #{task['id']}  {task['title']}  [{task['priority']}]  source=inspector")
    for skipped in result["skipped"]:
        echo(f"  [dim]跳过: {skipped['title']} ({skipped['reason']})[/dim]")
    for drop in dropped:
        echo(f"  [dim]过滤: {drop['title']} ({drop['reason']})[/dim]")


def _print_round_header(*, project_name: str, planner: str, agent: str, max_new_tasks: int, signals: tuple[str, ...], once: bool, round_num: int) -> None:
    head = f"巡检项目 {project_name}"
    if not once:
        head += f"  第 {round_num} 轮"
    echo(
        f"[cyan]{head}[/cyan]  planner={planner}  agent={agent}  "
        f"max={max_new_tasks}  signals={','.join(signals)}"
    )


def _emit_inspection_result(result: dict, *, dry_run: bool, json_mode: bool) -> None:
    if json_mode:
        payload = dict(result)
        error = payload.pop("error", None)
        emit_json_payload(
            "inspect",
            ok=not bool(error),
            data=payload,
            error=str(error) if error else None,
            error_code="inspect_failed" if error else None,
        )
        return
    _print_result(result, dry_run)


@click.command("inspect")
@click.option("--project", "-p", callback=_resolve_project_strict, help="项目名称")
@click.option("--max", "max_new", type=int, default=None, help="本轮最多新增任务数")
@click.option("--dry-run", is_flag=True, help="只打印候选，不落库")
@click.option("--agent", default="codex", help="给新任务指定执行智能体（默认 codex，负责写代码）")
@click.option(
    "--planner",
    default=None,
    help="巡检用的 LLM；优先级：显式参数 > [inspect].planner > [agents].planner > codex",
)
@click.option("--json", "json_mode", is_flag=True, help="以 JSON 输出结果，便于脚本和其他 AI 调用")
@click.option("--interval", type=int, default=None, help="巡检间隔秒数（默认 1800）")
@click.option("--once", is_flag=True, help="仅巡检一次后退出")
@click.option("--foreground", is_flag=True, help="以前台持续巡检模式运行")
@click.option("--status", "show_status", is_flag=True, help="查看项目巡检进程状态")
@click.option("--stop", "stop_service", is_flag=True, help="停止项目巡检进程")
@click.pass_context
def inspect(
    ctx: click.Context,
    project: str,
    max_new: Optional[int],
    dry_run: bool,
    agent: str,
    planner: Optional[str],
    json_mode: bool,
    interval: Optional[int],
    once: bool,
    foreground: bool,
    show_status: bool,
    stop_service: bool,
) -> None:
    """扫描项目信号，将可优化点作为候选任务产出.

    不带 --once 时将持续运行，每轮之间间隔 *interval* 秒（默认 1800 = 30 分钟）.
    传入 --once 则只巡检一轮后退出.
    """
    db.init_db()
    json_mode = resolve_json_mode(ctx, json_mode)
    proj = db.get_project(project) if project else None
    if not proj:
        if json_mode:
            emit_json_payload(
                "inspect",
                ok=False,
                data={"project": project or "", "created": [], "skipped": [], "candidates_total": 0},
                error="需要用 -p 指定项目，或先 codepilot init",
                error_code="project_required",
            )
            ctx.exit(1)
            return
        raise click.ClickException("需要用 -p 指定项目，或先 codepilot init")

    lifecycle_options = inspect_lifecycle.InspectServiceLifecycleOptions(
        project=project,
        max_new=max_new,
        dry_run=dry_run,
        agent=agent,
        planner=planner,
        interval=interval,
        once=once,
        foreground=foreground,
        json_mode=json_mode,
        show_status=show_status,
        stop_service=stop_service,
    )
    try:
        handled, exit_code = inspect_lifecycle.handle_service_lifecycle(
            lifecycle_options,
            inspect_service_status_fn=inspect_service_status,
            start_inspect_service_fn=start_inspect_service,
            stop_inspect_service_fn=stop_inspect_service,
            emit_json_payload_fn=emit_json_payload,
            echo_fn=echo,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if handled:
        if exit_code is not None:
            ctx.exit(exit_code)
        return

    project_info = {"name": proj["name"], "path": proj["path"], "config_file": proj.get("config_file")}

    cfg = load_project_config(proj)
    ins = cfg.inspect
    limit = max_new if max_new is not None else ins.max_new_tasks_per_round
    sleep_seconds = interval if interval is not None else ins.interval_seconds
    effective_planner = resolve_planner(cfg, "inspect", explicit=planner)

    loop_options = inspect_lifecycle.ForegroundInspectLoopOptions(
        project=project,
        project_info=project_info,
        signals=ins.signals,
        max_new_tasks=limit,
        interval_seconds=sleep_seconds,
        planner=effective_planner,
        priority=ins.priority,
        auto_execute=ins.auto_execute,
        agent=agent,
        dry_run=dry_run,
        once=once,
        json_mode=json_mode,
    )
    inspect_lifecycle.run_foreground_inspection_loop(
        loop_options,
        touch_service_state_fn=db.touch_service_state,
        clear_service_state_fn=db.clear_service_state,
        service_log_path_fn=lambda name: str(_service_log_path(name)),
        print_round_header_fn=_print_round_header,
        run_inspection_fn=run_inspection,
        emit_inspection_result_fn=_emit_inspection_result,
        echo_fn=echo,
    )


