"""Periodic codebase inspection: surface improvement candidates and enqueue them."""

from __future__ import annotations

import hashlib
import contextlib
import io
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

import click

from codepilot.storage import database as db
from codepilot.ai_support.service import (
    API_PROVIDERS,
    _run_api_provider,
    _run_claude_schema_prompt,
    _run_codex_schema_prompt,
    _run_opencode_schema_prompt,
    build_task_markdown_from_plan,
    mark_provider_unavailable,
    normalize_agent_name,
)
from codepilot.commands.add import _resolve_project_strict
from codepilot.commands import inspect_lifecycle, inspect_service, inspect_signals
from codepilot.commands.inspect_workflow import ensure_candidate_id, write_inspect_workflow_context
from codepilot.commands.inspect_signals import lint_fingerprint, lint_group_key
from codepilot.commands.inspect_signal_collectors import (
    collect_failed_tasks,
    collect_git_log,
    collect_pytest_collect,
    collect_ruff,
)
from codepilot.commands.inspect_signal_collectors_code_metrics import collect_code_metrics
from codepilot.commands.inspect_signal_collectors_dependency_health import collect_dependency_health
from codepilot.commands.inspect_signal_collectors_shared import CODE_EXTS, SCAN_EXTS
from codepilot.commands.inspect_signal_collectors_todos import collect_todos
from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.config import load_project_config, resolve_planner
from codepilot.core.output import echo
from codepilot.core.paths import global_storage_root
from codepilot.core.runtime import codepilot_command, is_process_alive, stop_process_tree
from codepilot.core.service_launcher import append_log_header, spawn_detached_command_via_launcher as _spawn_detached_command_via_launcher
from codepilot.core.task_template import missing_task_template_sections
from codepilot.commands.task_quality import (
    INSPECT_FILLER_KEYWORDS as _GENERIC_FILLER_KEYWORDS,
    evidence_grounded_in,
    looks_generic,
)

INSPECT_STATE_DIR = global_storage_root() / "inspect"
SKIPPED_SIGNAL = "（跳过）"
_PROMOTE_INSPECT_REPORT_PREFIX = "promote_inspect_report_"


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


def request_inspect_service_start(
    project: str,
    *,
    wait_seconds: float = 10.0,
) -> dict:
    """Request inspect startup from a short-lived external CLI process."""
    if not project:
        raise RuntimeError("启动巡检必须指定项目。")
    existing = inspect_service_status(project)
    if existing["running"]:
        existing["started"] = False
        return existing

    log_file = _service_log_path(project)
    append_log_header(log_file, f"\n--- request-start {_now_iso()} project={project} via cli ---\n")
    cmd = codepilot_command("inspect", "--project", project)
    launcher_pid = _spawn_detached_command_via_launcher(cmd, log_file=log_file)

    deadline = time.monotonic() + max(float(wait_seconds), 0.0)
    last_status = inspect_service_status(project)
    while time.monotonic() < deadline:
        last_status = inspect_service_status(project)
        if last_status["running"]:
            last_status["started"] = True
            last_status["launcher_pid"] = launcher_pid
            return last_status
        time.sleep(0.2)

    last_status["started"] = False
    last_status["launcher_pid"] = launcher_pid
    raise RuntimeError(f"巡检启动请求已发出，但 {wait_seconds:g}s 内未进入运行状态。")


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
                    "files": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string"},
                        "description": "Repo-relative file paths explicitly named by the evidence. Empty files => candidate will be dropped.",
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {"type": "string"},
                        "description": "Concrete, evidence-specific acceptance checks. Avoid generic wording.",
                    },
                    "verification_commands": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {"type": "string"},
                        "description": "Commands or manual checks the executor should run for this candidate.",
                    },
                    "effort": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                        "description": "Rough implementation effort. 'small' = hours, 'medium' = day, 'large' = multi-day.",
                    },
                },
                # OpenAI strict structured-output: every object needs
                # additionalProperties=false AND ALL properties in `required`.
                "required": [
                    "title",
                    "goal",
                    "priority",
                    "rationale",
                    "kind",
                    "evidence",
                    "files",
                    "acceptance_criteria",
                    "verification_commands",
                    "effort",
                ],
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
- `代码规模与复杂度线索`: weak hotspots. Only production-file hotspots may become candidates, and P4/refactor items based only on code_metrics should usually be skipped. Test-file hotspots are reference-only and must not create work by themselves.

[Hard rules]
- Each candidate MUST cite `evidence` referencing a concrete line/file/commit from the signals. No evidence → drop the candidate yourself; do not emit it.
- Each candidate MUST include non-empty `files` with repo-relative paths that appear in the evidence. No real file path → do not emit it.
- Each candidate MUST include concrete `acceptance_criteria` and `verification_commands` tied to those files/signals.
- `title` ≤ 80 characters; `goal` = 2–3 sentences covering "what to do + why".
- Do NOT propose generic items like "补一下文档", "加日志", "通用优化", "重构一下" unless the signal names the exact target.
- Do NOT repeat anything in the existing-tasks list below.
- Fill `effort` honestly; prefer `small`/`medium`. If it smells like `large`, split or skip.

[Language rules]
{language_rules}

## 已存在任务（backlog / in-progress）
{existing_titles}

{signal_sections}

[Output]
Return strict JSON in this shape:
{{"candidates": [{{"title": "...", "goal": "...", "priority": "P3", "rationale": "...", "kind": "refactor", "evidence": "signal 3: codepilot/foo.py:42 TODO ...", "files": ["codepilot/foo.py"], "acceptance_criteria": ["codepilot/foo.py:42 的 TODO 已处理"], "verification_commands": ["pytest -n auto --dist loadfile -m \"not slow\" tests/test_foo.py -q"], "effort": "small"}}]}}
If nothing is worth surfacing, return: {{"candidates": []}}
"""


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
    stream_callback: Callable[[str], None] | None = None,
) -> dict:
    # 1) Prefer the configured classifier provider (API with key).
    if classifier_provider and classifier_provider in API_PROVIDERS:
        from dataclasses import replace

        provider = replace(API_PROVIDERS[classifier_provider])
        if classifier_model:
            provider.model = classifier_model
            if hasattr(provider, "auto_model_selection"):
                provider.auto_model_selection = False
        if api_key:
            provider.api_key = api_key
        if base_url:
            provider.base_url = base_url
        if provider.requires_api_key() and not provider.resolve_api_key():
            mark_provider_unavailable(
                classifier_provider,
                provider,
                "missing api key",
                source="inspect",
                project_path=project_path,
            )
        else:
            try:
                raw = _run_api_provider(provider, prompt).strip()
            except Exception as exc:
                mark_provider_unavailable(
                    classifier_provider,
                    provider,
                    str(exc),
                    source="inspect",
                    project_path=project_path,
                )
            else:
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
    # Dispatch via the family registry so opencode (and future families) work too.
    from codepilot.ai_support.cli_families import get_family

    normalized = normalize_agent_name(planner) if planner else "claude"
    family = get_family(normalized)
    family_name = family.name if family else None
    if family_name is None:
        lowered = normalized.strip().lower()
        for candidate in ("claude", "codex", "opencode"):
            if lowered.startswith(candidate + "-") or lowered == candidate:
                family_name = candidate
                break

    if family_name == "claude":
        return _run_claude_schema_prompt(
            prompt,
            INSPECT_SCHEMA,
            planner=normalized,
            project_path=project_path,
            timeout=timeout,
            stream_callback=stream_callback,
        )
    if family_name == "opencode":
        return _run_opencode_schema_prompt(
            prompt,
            INSPECT_SCHEMA,
            project_path=project_path,
            timeout=timeout,
            stream_callback=stream_callback,
        )
    return _run_codex_schema_prompt(
        prompt,
        INSPECT_SCHEMA,
        project_path=project_path,
        timeout=timeout,
        stream_callback=stream_callback,
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
    language: str = "en",
) -> str:
    existing = _existing_titles(project_name)
    if str(language or "en").strip().lower() in {"zh", "zh-cn", "chinese", "中文"}:
        language_rules = (
            "- Instruction language is English (above).\n"
            "- The following output fields MUST be Chinese: `title`, `goal`, `rationale`.\n"
            "- The following output fields are fixed vocabulary (English): `priority` ∈ P1–P4, `kind` ∈ enum, `effort` ∈ enum.\n"
            "- `evidence` may be Chinese or English, but must point at a signal line."
        )
    else:
        language_rules = (
            "- Instruction language is English.\n"
            "- The following output fields MUST be English: `title`, `goal`, `rationale`.\n"
            "- The following output fields are fixed vocabulary (English): `priority` ∈ P1–P4, `kind` ∈ enum, `effort` ∈ enum.\n"
            "- `evidence` must point at a signal line and may quote raw signal text when needed."
        )
    return INSPECT_PROMPT.format(
        project_name=project_name,
        max_tasks=max_new_tasks,
        existing_titles=existing,
        signal_sections=_render_signal_sections(signal_results),
        language_rules=language_rules,
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


def _enabled_signal_keys(signal_results: list[InspectSignalResult]) -> list[str]:
    return [result.key for result in sorted(signal_results, key=lambda item: item.order) if result.enabled]


def _substantive_signal_keys(signal_results: list[InspectSignalResult]) -> list[str]:
    return [
        result.key
        for result in sorted(signal_results, key=lambda item: item.order)
        if result.enabled and not _is_empty_signal_content(result.content)
    ]


def _has_substantive_signal(signal_results: list[InspectSignalResult]) -> bool:
    return bool(_substantive_signal_keys(signal_results))


def _count_by_reason(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        reason = str(item.get("reason") or "unknown").strip() or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _build_quality_summary(
    signal_results: list[InspectSignalResult],
    *,
    raw_candidates_count: int = 0,
    kept_candidates_count: int = 0,
    actionable_candidates_count: int = 0,
    grouped_candidates_count: int | None = None,
    materialized_candidates_count: int | None = None,
    created: list[dict] | None = None,
    skipped: list[dict] | None = None,
    dropped: list[dict] | None = None,
    report_only: list[dict] | None = None,
    feedback_adjusted: list[dict] | None = None,
    llm_decision: str = "completed",
) -> dict:
    created = created or []
    skipped = skipped or []
    dropped = dropped or []
    report_only = report_only or []
    feedback_adjusted = feedback_adjusted or []
    enabled_signals = _enabled_signal_keys(signal_results)
    substantive_signals = _substantive_signal_keys(signal_results)
    signal_decision = "substantive_signals" if substantive_signals else "no_substantive_signals"
    if materialized_candidates_count is None:
        materialized_candidates_count = grouped_candidates_count
        if materialized_candidates_count is None:
            materialized_candidates_count = actionable_candidates_count

    decision_chain = [
        {
            "stage": "signals",
            "decision": signal_decision,
            "enabled": len(enabled_signals),
            "substantive": len(substantive_signals),
        },
        {
            "stage": "llm",
            "decision": llm_decision,
            "raw_candidates": raw_candidates_count,
        },
        {
            "stage": "filter",
            "input": raw_candidates_count,
            "kept": kept_candidates_count,
            "dropped": len(dropped),
        },
        {
            "stage": "report_only",
            "input": kept_candidates_count,
            "actionable": actionable_candidates_count,
            "report_only": len(report_only),
        },
    ]
    if feedback_adjusted:
        decision_chain.append(
            {
                "stage": "memory_feedback",
                "adjusted": len(feedback_adjusted),
                "promoted": sum(1 for item in feedback_adjusted if item.get("to") == "actionable"),
                "demoted": sum(1 for item in feedback_adjusted if item.get("to") == "report_only"),
            }
        )
    if grouped_candidates_count is not None:
        decision_chain.append(
            {
                "stage": "grouping",
                "input": actionable_candidates_count,
                "output": grouped_candidates_count,
            }
        )
    decision_chain.append(
        {
            "stage": "materialize",
            "input": materialized_candidates_count,
            "created": len(created),
            "skipped": len(skipped),
        }
    )

    return {
        "enabled_signals": enabled_signals,
        "substantive_signals": substantive_signals,
        "enabled_signal_count": len(enabled_signals),
        "substantive_signal_count": len(substantive_signals),
        "raw_candidates": raw_candidates_count,
        "kept_candidates": kept_candidates_count,
        "actionable_candidates": actionable_candidates_count,
        "materialized_candidates": materialized_candidates_count,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "dropped_count": len(dropped),
        "report_only_count": len(report_only),
        "dropped_by_reason": _count_by_reason(dropped),
        "skipped_by_reason": _count_by_reason(skipped),
        "report_only_by_reason": _count_by_reason(report_only),
        "feedback_adjusted": feedback_adjusted,
        "decision_chain": decision_chain,
    }


def _extract_candidates(payload: dict) -> list[dict]:
    candidates = payload.get("candidates") or []
    return candidates if isinstance(candidates, list) else []


_CANDIDATE_PATH_EXTS = tuple(sorted({*CODE_EXTS, *SCAN_EXTS}, key=len, reverse=True))
_CANDIDATE_PATH_RE = re.compile(
    r"(?<![\w./\\-])"
    r"((?:[\w.-]+[\\/])+[\w.-]+(?:"
    + "|".join(re.escape(ext) for ext in _CANDIDATE_PATH_EXTS)
    + r")|[\w.-]+(?:"
    + "|".join(re.escape(ext) for ext in _CANDIDATE_PATH_EXTS)
    + r"))"
    r"(?::\d+(?::\d+)?)?"
)
_GIT_LOG_REPORT_ONLY_ABNORMAL_RE = re.compile(
    r"\b(revert|wip|work in progress|fix[-_ ]?failed|failed|failure|rollback|hotfix|broken)\b"
    r"|回滚|撤销|失败|异常|修复失败",
    re.IGNORECASE,
)


def _normalize_candidate_path(raw: object, *, project_path: Path | None = None, require_existing: bool = False) -> str | None:
    text = str(raw or "").strip().strip("`'\".,;()[]{}")
    if not text:
        return None
    text = re.sub(r":\d+(?::\d+)?$", "", text.replace("\\", "/"))
    path = Path(text)
    if path.suffix.lower() not in _CANDIDATE_PATH_EXTS:
        return None
    if any(part == ".." for part in path.parts):
        return None
    if path.is_absolute():
        if project_path is None:
            return None
        try:
            path = path.resolve().relative_to(project_path.resolve())
        except Exception:
            return None
    rel = path.as_posix().lstrip("/")
    if not rel:
        return None
    if project_path is not None and require_existing:
        try:
            target = (project_path / rel).resolve()
            target.relative_to(project_path.resolve())
        except Exception:
            return None
        if not target.exists():
            return None
    return rel


def _extract_paths_from_text(text: object, *, project_path: Path | None = None, require_existing: bool = False) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in _CANDIDATE_PATH_RE.finditer(str(text or "")):
        rel = _normalize_candidate_path(
            match.group(1),
            project_path=project_path,
            require_existing=require_existing,
        )
        if rel and rel not in seen:
            paths.append(rel)
            seen.add(rel)
    return paths


def _candidate_files(
    item: dict,
    *,
    project_path: Path | None = None,
    require_existing: bool = False,
) -> list[str]:
    raw_files = item.get("files") if isinstance(item.get("files"), list) else []
    paths: list[str] = []
    seen: set[str] = set()
    for raw in raw_files:
        rel = _normalize_candidate_path(raw, project_path=project_path, require_existing=require_existing)
        if rel and rel not in seen:
            paths.append(rel)
            seen.add(rel)
    for field in ("evidence", "goal", "rationale"):
        for rel in _extract_paths_from_text(
            item.get(field),
            project_path=project_path,
            require_existing=require_existing,
        ):
            if rel not in seen:
                paths.append(rel)
                seen.add(rel)
    return paths


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


def _candidate_signal_keys(evidence: str, signal_results: list[InspectSignalResult]) -> set[str]:
    keys: set[str] = set()
    for result in signal_results:
        if not result.enabled or _is_empty_signal_content(result.content):
            continue
        tokens = {result.title, result.key, f"signal {result.order}", f"信号 {result.order}"}
        for line in result.content.splitlines():
            stripped = line.strip(" -#*`")
            if len(stripped) >= 4:
                tokens.add(stripped)
        if evidence_grounded_in(evidence, tokens):
            keys.add(result.key)
    return keys


def _filter_candidates(
    candidates: list[dict],
    *,
    signal_results: list[InspectSignalResult],
    project_path: Path | None = None,
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
        files = _candidate_files(item, project_path=project_path, require_existing=project_path is not None)
        if not files:
            loose_files = _candidate_files(item)
            dropped.append({"title": title, "reason": "files_not_found" if loose_files and project_path is not None else "missing_files"})
            continue
        item["files"] = files
        kept.append(item)
    return kept, dropped


def _report_only_entry(item: dict, *, reason: str, signal_results: list[InspectSignalResult]) -> dict:
    entry = {
        "title": (item.get("title") or "").strip(),
        "goal": (item.get("goal") or "").strip(),
        "priority": (item.get("priority") or "").strip() or "P3",
        "files": _string_list(item.get("files")) or _candidate_files(item),
        "reason": reason,
        "evidence": (item.get("evidence") or "").strip(),
        "kind": (item.get("kind") or "").strip() or "chore",
        "effort": (item.get("effort") or "").strip() or "small",
        "rationale": (item.get("rationale") or "").strip(),
        "acceptance_criteria": _string_list(item.get("acceptance_criteria")),
        "verification_commands": _string_list(item.get("verification_commands")),
        "signal_keys": sorted(_candidate_signal_keys(str(item.get("evidence") or ""), signal_results)),
    }
    ensure_candidate_id(entry, reason=reason)
    return entry


def _git_log_candidate_has_abnormal_terms(item: dict, signal_results: list[InspectSignalResult]) -> bool:
    chunks = [
        item.get("title") or "",
        item.get("goal") or "",
        item.get("evidence") or "",
    ]
    chunks.extend(result.content for result in signal_results if result.key == "git_log" and result.enabled)
    return bool(_GIT_LOG_REPORT_ONLY_ABNORMAL_RE.search("\n".join(str(chunk) for chunk in chunks)))


def _report_only_reason(item: dict, *, signal_results: list[InspectSignalResult]) -> str | None:
    priority = str(item.get("priority") or "").strip().upper()
    if priority == "P4":
        return "priority_p4_report_only"
    evidence = (item.get("evidence") or "").strip()
    signal_keys = _candidate_signal_keys(evidence, signal_results)
    if signal_keys == {"code_metrics"}:
        return "code_metrics_only_weak_signal"
    if signal_keys == {"git_log"} and not _git_log_candidate_has_abnormal_terms(item, signal_results):
        return "git_log_only_benign"
    return None


def _partition_report_only_candidates(
    candidates: list[dict],
    *,
    signal_results: list[InspectSignalResult],
) -> tuple[list[dict], list[dict]]:
    actionable: list[dict] = []
    report_only: list[dict] = []
    for item in candidates:
        reason = _report_only_reason(item, signal_results=signal_results)
        if reason:
            report_only.append(_report_only_entry(item, reason=reason, signal_results=signal_results))
            continue
        actionable.append(item)
    return actionable, report_only


def _memory_feedback_title(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _memory_feedback_score(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 50


def _memory_feedback_snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_candidate_id": candidate.get("candidate_id") or "",
        "feedback": str(candidate.get("feedback") or "neutral"),
        "score": _memory_feedback_score(candidate.get("score")),
        "signals": list(candidate.get("signals") or []),
        "last_seen_at": str(candidate.get("last_seen_at") or candidate.get("created_at") or ""),
        "source_event_ids": list(candidate.get("source_event_ids") or []),
    }


def _prefer_latest_feedback(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return incoming
    if str(incoming.get("last_seen_at") or "") >= str(existing.get("last_seen_at") or ""):
        return incoming
    return existing


def _inspect_memory_feedback_index(project_info: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    try:
        from codepilot.core.memory import read_memory_candidates
    except Exception:
        return {"candidate": {}, "title": {}}

    by_candidate: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    try:
        candidates = read_memory_candidates(project_info, limit=0)
    except Exception:
        return {"candidate": by_candidate, "title": by_title}

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        feedback = str(candidate.get("feedback") or "neutral")
        if feedback not in {"positive", "negative"}:
            continue
        snapshot = _memory_feedback_snapshot(candidate)
        details = candidate.get("details") if isinstance(candidate.get("details"), dict) else {}
        action_id = str(details.get("action_id") or "")
        if action_id.startswith(_PROMOTE_INSPECT_REPORT_PREFIX):
            inspect_candidate_id = action_id[len(_PROMOTE_INSPECT_REPORT_PREFIX) :]
            by_candidate[inspect_candidate_id] = _prefer_latest_feedback(
                by_candidate.get(inspect_candidate_id),
                snapshot,
            )
        title_key = _memory_feedback_title(details.get("title") or candidate.get("summary"))
        if title_key:
            by_title[title_key] = _prefer_latest_feedback(by_title.get(title_key), snapshot)

    return {"candidate": by_candidate, "title": by_title}


def _feedback_for_inspect_candidate(
    item: dict,
    *,
    reason: str,
    feedback_index: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    candidate_id = ensure_candidate_id(item, reason=reason)
    by_candidate = feedback_index.get("candidate") or {}
    if candidate_id in by_candidate:
        return dict(by_candidate[candidate_id])
    by_title = feedback_index.get("title") or {}
    title_key = _memory_feedback_title(item.get("title"))
    if title_key in by_title:
        return dict(by_title[title_key])
    return None


def _feedback_adjustment(
    item: dict,
    *,
    feedback: dict[str, Any],
    from_state: str,
    to_state: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "candidate_id": item.get("candidate_id") or "",
        "title": str(item.get("title") or ""),
        "from": from_state,
        "to": to_state,
        "reason": reason,
        "feedback": feedback.get("feedback") or "neutral",
        "score": feedback.get("score"),
        "memory_candidate_id": feedback.get("memory_candidate_id") or "",
    }


def _apply_memory_feedback_to_candidates(
    project_info: dict[str, Any],
    actionable_candidates: list[dict],
    report_only: list[dict],
    *,
    signal_results: list[InspectSignalResult],
) -> tuple[list[dict], list[dict], list[dict]]:
    feedback_index = _inspect_memory_feedback_index(project_info)
    adjusted: list[dict] = []
    next_actionable: list[dict] = []
    next_report_only: list[dict] = []

    for item in actionable_candidates:
        feedback = _feedback_for_inspect_candidate(item, reason="actionable", feedback_index=feedback_index)
        if feedback and feedback.get("feedback") == "negative" and _memory_feedback_score(feedback.get("score")) <= 40:
            demoted = _report_only_entry(item, reason="memory_negative_feedback", signal_results=signal_results)
            demoted["memory_feedback"] = feedback
            next_report_only.append(demoted)
            adjusted.append(
                _feedback_adjustment(
                    demoted,
                    feedback=feedback,
                    from_state="actionable",
                    to_state="report_only",
                    reason="negative_memory_feedback",
                )
            )
            continue
        if feedback:
            item["memory_feedback"] = feedback
        next_actionable.append(item)

    for item in report_only:
        reason = str(item.get("reason") or "report_only")
        feedback = _feedback_for_inspect_candidate(item, reason=reason, feedback_index=feedback_index)
        if feedback and feedback.get("feedback") == "positive" and _memory_feedback_score(feedback.get("score")) >= 80:
            promoted = dict(item)
            if str(promoted.get("priority") or "").upper() == "P4":
                promoted["priority"] = "P3"
            promoted["memory_feedback"] = feedback
            next_actionable.append(promoted)
            adjusted.append(
                _feedback_adjustment(
                    promoted,
                    feedback=feedback,
                    from_state="report_only",
                    to_state="actionable",
                    reason="positive_memory_feedback",
                )
            )
            continue
        next_report_only.append(item)

    return next_actionable, next_report_only, adjusted


def _group_lint_candidates(candidates: list[dict]) -> list[dict]:
    """Merge same-category low-priority lint candidates into batch tasks.

    P1/P2 candidates are never grouped — they represent actionable
    blockers (test collect failures, unavailable executors, service
    outages) and must remain individually visible.
    """
    groups: dict[str, list[dict]] = {}
    standalone: list[dict] = []

    for c in candidates:
        priority = (c.get("priority") or "P3").strip()
        evidence = (c.get("evidence") or "").strip()

        if priority in ("P1", "P2"):
            standalone.append(c)
            continue

        code = lint_group_key(evidence)
        if code:
            groups.setdefault(code, []).append(c)
        else:
            standalone.append(c)

    result = list(standalone)
    for code, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
        else:
            result.append(_merge_lint_group(group, code))
    return result


def _merge_lint_group(group: list[dict], code: str) -> dict:
    """Merge multiple same-code lint candidates into one batch candidate."""
    titles = [c.get("title") or "" for c in group]
    evidences = [c.get("evidence") or "" for c in group]
    return {
        "title": f"批量清理 {code} lint 警告",
        "goal": f"统一处理本轮巡检发现的 {len(group)} 处 {code} lint 问题。",
        "priority": "P3",
        "rationale": f"合并 {len(group)} 条同类 lint 信号 ({', '.join(titles[:3])})",
        "kind": "chore",
        "evidence": "; ".join(evidences[:5]),
        "effort": "small",
    }


def _any_task_has_fingerprint(tasks: list[dict], fingerprint: str) -> bool:
    """Check if any existing open inspector task content contains the fingerprint."""
    marker = f"源指纹: {fingerprint}"
    for t in tasks:
        if t.get("source") != "inspector":
            continue
        if t.get("status") not in {"backlog", "in_progress"}:
            continue
        content = t.get("content") or ""
        if marker in content:
            return True
    return False


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
    existing_tasks = db.list_tasks(project=project_name)
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
        evidence = (item.get("evidence") or "").strip()
        code = lint_group_key(evidence)
        fp = lint_fingerprint(code) if code else None
        if fp and _any_task_has_fingerprint(existing_tasks, fp):
            skipped.append({"title": title, "reason": "duplicate_fingerprint"})
            continue
        files = _candidate_files(item, project_path=project_path, require_existing=True)
        if not files:
            loose_files = _candidate_files(item)
            skipped.append({"title": title, "reason": "files_not_found" if loose_files else "missing_files"})
            continue
        item["files"] = files
        content = _build_content(item, agent=agent, default_priority=priority, fingerprint=fp)
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
            preview = {"title": title, "goal": goal, "priority": item.get("priority") or priority, "files": files}
            if item.get("candidate_id"):
                preview.update(
                    {
                        "candidate_id": item.get("candidate_id") or "",
                        "rationale": (item.get("rationale") or "").strip(),
                        "kind": (item.get("kind") or "").strip() or "chore",
                        "evidence": evidence,
                        "acceptance_criteria": _string_list(item.get("acceptance_criteria")),
                        "verification_commands": _string_list(item.get("verification_commands")),
                        "effort": (item.get("effort") or "").strip() or "small",
                        "signal_keys": _string_list(item.get("signal_keys")),
                    }
                )
            created.append(preview)
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


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _default_acceptance_criteria(item: dict, *, files: list[str], evidence: str) -> list[str]:
    target = ", ".join(files[:3]) if files else "巡检证据指向的文件"
    evidence_head = evidence.replace("\n", " ")[:140] if evidence else "巡检证据"
    return [
        f"{target} 中由 `{evidence_head}` 指向的问题已被实际处理。",
        "实现范围限制在 Files In Scope 和必要配套测试内，没有扩散到无关模块。",
    ]


def _default_verification_commands(item: dict, *, files: list[str]) -> list[str]:
    evidence = str(item.get("evidence") or "").lower()
    if "ruff" in evidence or lint_group_key(str(item.get("evidence") or "")):
        return [f"ruff check {' '.join(files)}"] if files else ["ruff check ."]
    if any(path.endswith(".py") for path in files):
        return ['pytest -n auto --dist loadfile -m "not slow" -q']
    return ["git diff --check"]


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
    stream_callback: Callable[[str], None] | None = None,
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
            "report_only": [],
            "report_only_count": 0,
            "quality_summary": _build_quality_summary(
                signal_results,
                llm_decision="skipped_no_substantive_signals",
            ),
            "auto_execute": auto_execute,
            "reason": "no_substantive_signals",
            "note": "本轮所有信号都是空/跳过，不触发 LLM，避免硬规划填充任务。",
        }

    cfg = load_project_config(project_info)
    prompt = _build_inspection_prompt(
        project_name=project_name,
        max_new_tasks=max_new_tasks,
        signal_results=signal_results,
        language=str(getattr(getattr(cfg, "automation", None), "agent_language", "en") or "en"),
    )

    provider_key = ""
    model = ""
    api_key = None
    base_url = None

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
            stream_callback=stream_callback,
        )
    except Exception as exc:
        return {
            "project": project_name,
            "error": f"巡检 LLM 调用失败：{exc}",
            "candidates_total": 0,
            "created": [],
            "skipped": [],
            "dropped": [],
            "report_only": [],
            "report_only_count": 0,
            "quality_summary": _build_quality_summary(
                signal_results,
                llm_decision="error",
            ),
        }

    raw_candidates = _extract_candidates(payload)
    kept_candidates, dropped = _filter_candidates(
        raw_candidates,
        signal_results=signal_results,
        project_path=project_path,
    )
    actionable_candidates, report_only = _partition_report_only_candidates(
        kept_candidates,
        signal_results=signal_results,
    )
    actionable_candidates, report_only, feedback_adjusted = _apply_memory_feedback_to_candidates(
        project_info,
        actionable_candidates,
        report_only,
        signal_results=signal_results,
    )
    task_candidates = _group_lint_candidates(actionable_candidates)
    for item in task_candidates:
        if isinstance(item, dict):
            item["signal_keys"] = sorted(_candidate_signal_keys(str(item.get("evidence") or ""), signal_results))
            ensure_candidate_id(item, reason="actionable")
    created, skipped = _materialize_inspection_output(
        task_candidates,
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
        "report_only": report_only,
        "report_only_count": len(report_only),
        "quality_summary": _build_quality_summary(
            signal_results,
            raw_candidates_count=len(raw_candidates),
            kept_candidates_count=len(kept_candidates),
            actionable_candidates_count=len(actionable_candidates),
            grouped_candidates_count=len(task_candidates),
            materialized_candidates_count=len(task_candidates),
            created=created,
            skipped=skipped,
            dropped=dropped,
            report_only=report_only,
            feedback_adjusted=feedback_adjusted,
            llm_decision="completed",
        ),
        "auto_execute": auto_execute,
    }


def _build_content(item: dict, *, agent: str = "codex", default_priority: str = "P3", fingerprint: str | None = None) -> str:
    kind = item.get("kind") or "chore"
    effort = (item.get("effort") or "small").strip() or "small"
    rationale = (item.get("rationale") or "").strip()
    goal = (item.get("goal") or "").strip()
    evidence = (item.get("evidence") or "").strip()
    files = _candidate_files(item) or _string_list(item.get("files"))
    acceptance_criteria = _string_list(item.get("acceptance_criteria")) or _default_acceptance_criteria(
        item,
        files=files,
        evidence=evidence,
    )
    verification_commands = _string_list(item.get("verification_commands")) or _default_verification_commands(
        item,
        files=files,
    )
    notes = [
        f"由 `codepilot inspect` 自动建议（kind={kind}, effort={effort}）。",
        "执行前请人工确认方向与优先级。",
    ] + ([f"补充背景：{rationale}"] if rationale else [])
    if fingerprint:
        notes.append(f"源指纹: {fingerprint}")
    task_spec = {
        "title": (item.get("title") or "").strip(),
        "agent": agent,
        "priority": (item.get("priority") or "").strip() or default_priority,
        "goal": goal or "根据巡检信号完成针对性处理，并消除对应风险。",
        "acceptance_criteria": acceptance_criteria,
        "verification_commands": verification_commands,
        "builder_notes": [
            f"优先按巡检建议执行最小闭环改动（kind={kind}, effort={effort}）。",
            rationale or "结合信号内容确认具体处理路径，再开始实现。",
            "验证命令：" + " && ".join(verification_commands),
        ],
        "reviewer_notes": [
            "逐项核对 Planning Evidence 是否被真实引用到改动与验证里。",
            "确认 Verification Matrix 中的命令已执行或有明确不能执行的原因。",
            "确认实现没有超出本次巡检建议的目标与边界。",
        ],
        "files": files,
        "notes": notes,
        "forbidden": [
            "不要因为巡检建议而顺手做无关重构或范围外修补。",
        ],
        "not_in_scope": [
            "Files In Scope 之外的模块、文档、部署流程，除非验证所需的最小测试配套。",
        ],
        "evidence": evidence or "（未提供规划依据，建议人工复核）",
        "risk_level": "medium",
        "scope_budget": f"inspect:{effort}",
        "depends_on_indices": [],
    }
    return build_task_markdown_from_plan(task_spec)


def _format_reason_counts(counts: object) -> str:
    if not isinstance(counts, dict) or not counts:
        return "-"
    parts = []
    for reason, count in sorted(counts.items()):
        parts.append(f"{reason}={count}")
    return ", ".join(parts)


def _format_quality_summary_line(summary: object) -> str:
    if not isinstance(summary, dict) or not summary:
        return ""
    return (
        "[dim]质量摘要："
        f"信号 enabled={summary.get('enabled_signal_count', 0)}/"
        f"substantive={summary.get('substantive_signal_count', 0)}；"
        f"候选 raw={summary.get('raw_candidates', 0)}/"
        f"created={summary.get('created_count', 0)}/"
        f"report_only={summary.get('report_only_count', 0)}/"
        f"dropped={summary.get('dropped_count', 0)}/"
        f"skipped={summary.get('skipped_count', 0)}；"
        f"reason report_only={_format_reason_counts(summary.get('report_only_by_reason'))}，"
        f"dropped={_format_reason_counts(summary.get('dropped_by_reason'))}，"
        f"skipped={_format_reason_counts(summary.get('skipped_by_reason'))}"
        "[/dim]"
    )


def _print_result(result: dict, dry_run: bool) -> None:
    """Print a single inspection result to the terminal."""
    quality_line = _format_quality_summary_line(result.get("quality_summary"))
    if result.get("error"):
        echo(f"[red]{result['error']}[/red]")
        if quality_line:
            echo(quality_line)
        return

    if result.get("reason") == "no_substantive_signals":
        note = result.get("note") or "所有信号为空，不触发 LLM。"
        echo(f"[dim]{note}[/dim]")
        if quality_line:
            echo(quality_line)
        return

    dropped = result.get("dropped") or []
    report_only = result.get("report_only") or []
    echo(
        f"[green]候选总数 {result['candidates_total']}，"
        f"新建 {len(result['created'])}，跳过 {len(result['skipped'])}，"
        f"仅报告 {len(report_only)}，过滤 {len(dropped)}[/green]"
    )
    if quality_line:
        echo(quality_line)
    for task in result["created"]:
        if dry_run:
            echo(f"  [dim][dry-run][/dim] {task['title']}  [{task.get('priority')}]")
        else:
            echo(f"  #{task['id']}  {task['title']}  [{task['priority']}]  source=inspector")
    for report in report_only:
        files = ", ".join(report.get("files") or [])
        file_suffix = f" files={files}" if files else ""
        echo(
            f"  [dim]仅报告: {report['title']}  "
            f"[{report.get('priority')}] ({report['reason']}){file_suffix}[/dim]"
        )
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
@click.option("--write-workflow", is_flag=True, help="将 dry-run 巡检结果写入项目 workflow context")
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
    write_workflow: bool,
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
    if write_workflow and (not dry_run or not once):
        message = "--write-workflow 仅支持与 --once --dry-run 一起使用。"
        if json_mode:
            emit_json_payload("inspect", ok=False, data={}, error=message, error_code="invalid_options")
            ctx.exit(1)
            return
        raise click.ClickException(message)
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
    streamed = {"seen": False}

    def _stream_chunk(chunk: str) -> None:
        if not chunk:
            return
        streamed["seen"] = True
        click.echo(chunk, nl=False)

    def _run_inspection_with_stream(project_info: dict, **kwargs) -> dict:
        if json_mode:
            with contextlib.redirect_stdout(io.StringIO()):
                return run_inspection(
                    project_info,
                    **kwargs,
                    stream_callback=None,
                )
        return run_inspection(
            project_info,
            **kwargs,
            stream_callback=_stream_chunk,
        )

    def _emit_result_after_stream(result: dict, *, dry_run: bool, json_mode: bool) -> None:
        if streamed["seen"]:
            click.echo()
            streamed["seen"] = False
        if write_workflow:
            result["workflow_context"] = write_inspect_workflow_context(
                project_info,
                result,
                source_command=f"codepilot inspect -p {project_info['name']} --once --dry-run --write-workflow --json",
            )
        _emit_inspection_result(result, dry_run=dry_run, json_mode=json_mode)

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
        run_inspection_fn=_run_inspection_with_stream,
        emit_inspection_result_fn=_emit_result_after_stream,
        echo_fn=echo,
    )
