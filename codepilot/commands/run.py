"""Run queued tasks via external dispatch or a built-in executor.

Shell/command helpers live in `run_shell.py`; git operations live in
`run_git.py`. Both are re-exported here so existing imports continue to work.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from codepilot import db
from codepilot.ai import (
    _get_node_modules_path,
    check_provider_availability,
    normalize_agent_name,
    resolve_dual_phase_agents,
    resolve_cli_provider,
)
from codepilot.ai_gateway import GatewayRequest, call_structured
from codepilot.commands.status import _resolve_project, render_project_dashboard
from codepilot.config import (
    load_project_config,
    resolve_planner,
    resolve_project_config_reference,
)
from codepilot.output import echo, safe
from codepilot.paths import project_storage_root
from codepilot.prompts import load_prompt as _load_prompt
from codepilot.runtime import (
    HEARTBEAT_INTERVAL_SECONDS,
    clear_task_runtime,
    get_stop_request,
    list_live_tasks,
    reap_stalled_tasks,
    stop_process_tree,
    stop_worktree_leftovers,
    tail_text,
    update_task_runtime,
)
from codepilot.webhook import notify_task_status

# Re-export shell + command helpers
from codepilot.commands.run_shell import (  # noqa: F401
    PreflightSkipError,
    ShellInfo,
    TaskCancelled,
    build_script_command,
    detect_best_shell,
    _run_command,
    _run_command_live,
    _should_show_line,
    _summarize_output,
)
# Re-export git helpers
from codepilot.commands.run_git import (  # noqa: F401
    _git_auto_commit,
    _git_changed_files,
    _git_cleanup_task_worktree,
    _git_checkout,
    _git_current_branch,
    _git_has_changes,
    _git_is_repo,
    _git_local_branch_exists,
    _git_merge_task_branch,
    _git_merge_task_worktree,
    _git_prepare_task_worktree,
    _git_prune_worktrees,
    _git_prepare_task_branch,
    _git_worktree_exists,
    _git_list_worktrees,
    _resolve_project_base_branch,
    _resolve_project_worktree_base,
    _slugify_path_part,
    _slugify_branch_part,
    _task_worktree_path,
    _task_branch_name,
)
# Re-export built-in executor helpers
from codepilot.commands.run_builtin import (  # noqa: F401
    ExecutionResult,
    _PhaseOutcome,
    _ExecutorContext,
    _builtin_runtime_dir,
    _builtin_preflight_error,
    _builtin_base_branch_lock_error,
    _task_phase_override,
    _resolve_dual_phase_agents_for_task,
    _builtin_review_requires_git,
    _write_task_log,
    _read_output_file,
    _extract_task_sections,
    _bullet_lines,
    _collect_project_conventions_snippet,
    _build_builtin_prompt,
    _build_review_prompt,
    _extract_review_verdict,
    _resolve_builtin_single_agent,
    _resolve_builtin_phase_agent,
    _run_builtin_phase,
    _extract_reviewer_findings,
    _make_phase_output_path,
    _run_builder_round,
    _run_reviewer_round,
    _finalize_executor_success,
    _run_builtin_executor,
)
from codepilot.commands.run_orchestrator import run_backlog as _run_backlog_orchestrated

STATUS_CONSOLE = Console()




def _project_config(project_ref: str | dict | None):
    return load_project_config(project_ref)


def _find_dispatch_script(project_path: str | None = None) -> Optional[Path]:
    """Return the configured dispatch script if one exists."""
    candidates: list[Path] = []

    import os

    env_path = os.environ.get("CODEPILOT_DISPATCH_PATH")
    if env_path:
        candidates.append(Path(env_path))

    cfg = _project_config(project_path)
    if cfg and cfg.dispatch.dispatch_path:
        candidates.append(Path(cfg.dispatch.dispatch_path))

    if project_path:
        project_name = None
        if cfg and getattr(getattr(cfg, "project", None), "name", ""):
            project_name = cfg.project.name
        project_scripts = project_storage_root(project_name=project_name, project_path=project_path) / "scripts"
        candidates.append(project_scripts / "task-dispatch.ps1")
        candidates.append(project_scripts / "task-dispatch.sh")

    package_scripts = [
        Path(__file__).resolve().parent.parent / "scripts" / "task-dispatch.ps1",
        Path(__file__).resolve().parent.parent / "scripts" / "task-dispatch.sh",
    ]
    candidates.extend(package_scripts)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _is_powershell_script(script_path: Path) -> bool:
    return script_path.suffix.lower() in {".ps1", ".psm1"}


def _build_task_md(task: dict) -> str:
    """Build markdown consumed by the executor."""
    depends = task.get("depends_on") or ""
    if isinstance(depends, str):
        try:
            import json

            deps = json.loads(depends)
        except Exception:
            deps = []
    else:
        deps = depends or []

    depends_str = ", ".join(str(dep) for dep in deps) if deps else "无"
    content = task.get("content") or "## 任务描述\n\n（待填充）"
    return "\n".join(
        [
            f"# Task: {task['title']}",
            "",
            f"- **Agent:** {task['agent']}",
            f"- **Priority:** {task['priority']}",
            f"- **Depends on:** {depends_str}",
            "",
            content,
        ]
    )


def _pick_task_file(
    project_path: Path,
    task_id: int,
    tracked: bool = True,
    *,
    project: dict | None = None,
) -> Path:
    backlog = (
        project_path / "tasks" / "backlog"
        if tracked
        else project_storage_root(project, project_path=project_path) / "task-files"
    )
    backlog.mkdir(parents=True, exist_ok=True)
    return backlog / f"{task_id:03d}-task.md"



def _run_dispatch(project: dict, task_file: Path, agent_mode: str, shell: ShellInfo, dry_run: bool = False) -> tuple[int, str]:
    dispatch_path = _find_dispatch_script(str(project.get("path", "")))
    if not dispatch_path:
        raise FileNotFoundError("未找到 dispatch 脚本")

    script_args = ["-AgentMode", agent_mode, "-TaskFile", str(task_file), "-Once"]
    if dry_run:
        script_args.append("-DryRun")

    cmd, _ = build_script_command(shell, dispatch_path, script_args)
    runtime_dir = _builtin_runtime_dir(project)
    log_path = runtime_dir / f"task-{Path(task_file).stem}-dispatch.md"
    task_id = int(task_file.stem.split("-", 1)[0])
    return _run_command_live(
        cmd,
        task_id=task_id,
        phase="dispatch",
        log_path=log_path,
        timeout=3600,
    )


def _handle_failure(task: dict, error_message: str, stop_on_failure: bool = False) -> tuple[dict, bool]:
    updated = db.increment_task_retry(task["id"], error_message[:4000])
    should_stop = stop_on_failure or updated["status"] == "failed"
    return updated, should_stop


_DETERMINISTIC_FAILURE_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["merge", "discard"]},
        "matched_task_id": {"type": ["integer", "null"]},
        "rationale": {"type": "string"},
        "merged_note": {"type": "string"},
    },
    "required": ["action", "matched_task_id", "rationale", "merged_note"],
    "additionalProperties": False,
}

_DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT = 20


def _trim_triage_text(text: str, limit: int = 280) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _iter_triage_candidates(project: str, *, exclude_task_id: int) -> list[dict]:
    return [
        task
        for task in db.list_tasks(project=project)
        if task.get("id") != exclude_task_id and task.get("status") in {"backlog", "in_progress"}
    ]


def _build_deterministic_failure_triage_prompt(
    task: dict,
    *,
    error_message: str,
    candidates: list[dict],
) -> str:
    current_content = _trim_triage_text(task.get("content") or "", limit=500) or "（无）"
    current_error = _trim_triage_text(error_message, limit=500) or "（无）"
    candidate_lines: list[str] = []
    for candidate in candidates[:_DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT]:
        candidate_lines.append(
            "\n".join(
                [
                    f"- #{candidate['id']} [{candidate.get('status')}] {candidate.get('title', '')}",
                    f"  content: {_trim_triage_text(candidate.get('content') or '', limit=220) or '（无）'}",
                    f"  latest: {_trim_triage_text(candidate.get('error_message') or candidate.get('delivery_record') or '', limit=180) or '（无）'}",
                ]
            )
        )

    candidate_block = "\n".join(candidate_lines) if candidate_lines else "（无可归并候选）"
    return (
        "你在做 deterministic failure 的 AI triage。\n"
        "目标只有两个动作：\n"
        "1. merge：当前失败任务本质上已经被某个 backlog/in_progress 任务覆盖，应把失败上下文归并到那个任务。\n"
        "2. discard：当前失败不需要生成新的待办，也不该归并到任何现有任务。\n\n"
        "严格规则：\n"
        "- 只能返回 merge 或 discard。\n"
        "- 只有在候选任务明确覆盖当前失败问题时才能 merge。\n"
        "- matched_task_id 只能填候选列表里的任务 id；discard 时必须为 null。\n"
        "- merged_note 写成会追加到目标任务里的中文简述；discard 时写一句简短处置说明。\n"
        "- 不要建议 retry，不要建议新建任务，不要输出 JSON 以外的内容。\n\n"
        f"当前失败任务:\n"
        f"- id: {task.get('id')}\n"
        f"- title: {task.get('title', '')}\n"
        f"- priority: {task.get('priority', '')}\n"
        f"- content: {current_content}\n"
        f"- failure: {current_error}\n\n"
        "现有开放任务候选:\n"
        f"{candidate_block}\n\n"
        '输出 JSON: {"action":"merge|discard","matched_task_id":123|null,"rationale":"...","merged_note":"..."}'
    )


def _format_triage_merge_note(
    source_task: dict,
    *,
    error_message: str,
    rationale: str,
    merged_note: str,
) -> str:
    detail = _trim_triage_text(merged_note or rationale, limit=240) or "已归并失败上下文。"
    failure_head = _trim_triage_text(error_message, limit=240) or "（无）"
    return "\n".join(
        [
            "## AI triage 归并记录",
            "",
            f"- 来源任务: #{source_task.get('id')} {source_task.get('title', '')}",
            f"- 失败摘要: {failure_head}",
            f"- 归并说明: {detail}",
        ]
    )


def _append_task_content(base: str, note: str) -> str:
    base = (base or "").rstrip()
    note = (note or "").strip()
    if not base:
        return note
    if not note:
        return base
    return f"{base}\n\n{note}"


def _resolve_triage_project_context(task: dict) -> tuple[str, str, object | None]:
    project = db.get_project(task["project"])
    project_path = str(task.get("project_path") or "").strip()
    config_file = str(project.get("config_file") or "").strip() if project else ""
    if not project_path and project:
        project_path = str(project.get("path") or "").strip()

    project_ref = project or {"path": project_path, "config_file": config_file}
    config_ref = resolve_project_config_reference(project_ref)
    config = load_project_config(project_ref) if config_ref else None
    return project_path, config_ref, config


def _triage_deterministic_failure(task: dict, error_message: str) -> dict | None:
    candidates = _iter_triage_candidates(task["project"], exclude_task_id=task["id"])
    if not candidates:
        return None

    visible_candidates = candidates[:_DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT]
    visible_candidate_ids = {item["id"] for item in visible_candidates}
    project_path, config_ref, config = _resolve_triage_project_context(task)
    if not project_path:
        return None
    classifier = getattr(config, "classifier", None)
    provider_key = getattr(classifier, "provider", "") if classifier else ""
    model = getattr(classifier, "model", "") if classifier else ""
    timeout = int(getattr(classifier, "timeout", 30) or 30)
    api_key = config.get_provider_api_key(provider_key) if config and provider_key else None
    provider_cfg = config.providers.get(provider_key) if config and provider_key else None
    base_url = provider_cfg.base_url if provider_cfg else None
    planner = resolve_planner(config, "automation")
    prompt = _build_deterministic_failure_triage_prompt(
        task,
        error_message=error_message,
        candidates=visible_candidates,
    )

    try:
        response = call_structured(
            GatewayRequest(
                prompt=prompt,
                schema=_DETERMINISTIC_FAILURE_TRIAGE_SCHEMA,
                classifier_provider=provider_key,
                classifier_model=model,
                api_key=api_key,
                base_url=base_url,
                project_path=project_path,
                config_ref=config_ref,
                planner=planner,
                timeout=max(timeout, 15),
            )
        )
    except Exception:
        return None

    if not response.ok or not isinstance(response.payload, dict):
        return None

    action = str(response.payload.get("action") or "").strip().lower()
    rationale = str(response.payload.get("rationale") or "").strip()
    merged_note = str(response.payload.get("merged_note") or "").strip()
    matched_task_id = response.payload.get("matched_task_id")

    if action == "merge" and isinstance(matched_task_id, int) and matched_task_id in visible_candidate_ids:
        target = db.get_task(matched_task_id)
        if not target or target.get("status") not in {"backlog", "in_progress"}:
            return None
        merge_note = _format_triage_merge_note(
            task,
            error_message=error_message,
            rationale=rationale,
            merged_note=merged_note,
        )
        db.update_task(target["id"], content=_append_task_content(target.get("content") or "", merge_note))
        return {
            "action": "merge",
            "matched_task_id": target["id"],
            "matched_task_title": target.get("title", ""),
            "rationale": rationale,
            "note": merge_note,
            "source": response.source,
        }

    if action == "discard" and matched_task_id is None:
        return {
            "action": "discard",
            "matched_task_id": None,
            "matched_task_title": "",
            "rationale": rationale,
            "note": _trim_triage_text(merged_note or rationale, limit=240),
            "source": response.source,
        }

    return None


def _apply_deterministic_failure_triage(task: dict, error_message: str) -> str:
    decision = _triage_deterministic_failure(task, error_message)
    if not decision:
        return error_message

    if decision["action"] == "merge":
        triage_line = (
            f"AI triage: 已归并到 #{decision['matched_task_id']} "
            f"{decision['matched_task_title']}（{decision.get('rationale') or '已有待办覆盖'}）"
        )
    else:
        triage_line = f"AI triage: 已丢弃单独跟进（{decision.get('rationale') or '无需额外待办'}）"
    return f"{error_message}\n{triage_line}".strip()


def _mark_task_failed(task: dict, error_message: str) -> dict:
    current_retry = int(task.get("retry_count") or 0) + 1
    return clear_task_runtime(
        task["id"],
        status="failed",
        completed_at=datetime.now().isoformat(),
        retry_count=current_retry,
        error_message=error_message[:4000],
        stop_requested=0,
        stop_reason=None,
    )


def _cleanup_worktree_leftovers(
    worktree_path: Path | str | None,
    project_path: Path | str | None,
    *,
    task_id: int,
) -> None:
    """Best-effort: kill long-lived dev servers (``next dev`` / ``vite`` /
    ``npm run dev``) the builder agent left running in the task worktree.

    Only fires for real worktrees — if the task ran in-place on the project
    root we skip, otherwise we'd kill the user's own dev server. Failures
    are swallowed so a cleanup hiccup never masks the task's real result.
    """
    if not worktree_path:
        return
    try:
        wt = Path(worktree_path).resolve()
        pp = Path(project_path).resolve() if project_path else None
    except Exception:
        return
    if pp is not None and wt == pp:
        return
    try:
        killed = stop_worktree_leftovers(wt, wait_seconds=4)
    except Exception:
        return
    if killed:
        echo(f"[dim]任务 #{task_id} worktree 遗留进程已清理（PID={','.join(str(p) for p in killed)}）[/dim]")


def _tail_lines(text: str, max_lines: int = 12) -> list[str]:
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    return lines[-max_lines:]


def _show_failure_feedback(
    task_id: int,
    *,
    title: str,
    error_message: str,
    review_output: str = "",
    output: str = "",
    requeued: bool,
) -> None:
    action = "已回退到 backlog" if requeued else "已标记为 failed"
    echo(f"[red][X] 任务 #{task_id} 未通过[/red]  {title}")
    click.echo(f"  结果: {action}")
    click.echo(f"  原因: {error_message.splitlines()[0] if error_message else '执行失败'}")
    detail_lines = _summarize_output(review_output or output) or _tail_lines(review_output or output, max_lines=5)
    if detail_lines:
        echo("[dim]--- 摘要 ---[/dim]")
        for line in detail_lines[:5]:
            click.echo(f"  {line[:120]}")
    click.echo(f"  查看完整日志: codepilot logs {task_id} --full")


def run_backlog(
    project: str,
    *,
    once: bool = True,
    limit: int = 1,
    dry_run: bool = False,
    cleanup: bool = True,
    shell: str = "auto",
    executor: str = "auto",
    auto_commit: bool = True,
    retry_on_failure: bool = True,
    quiet: bool = False,
) -> dict:
    """Execute up to `limit` runnable tasks for a project."""
    return _run_backlog_orchestrated(
        project,
        once=once,
        limit=limit,
        dry_run=dry_run,
        cleanup=cleanup,
        shell=shell,
        executor=executor,
        auto_commit=auto_commit,
        retry_on_failure=retry_on_failure,
        quiet=quiet,
    )


@click.command("run")
@click.option("--project", "-p", callback=_resolve_project, help="项目名称")
@click.option("--once", is_flag=True, help="执行一个任务后退出")
@click.option("--limit", "-n", type=int, default=1, help="最多执行任务数量")
@click.option("--dry-run", is_flag=True, help="试运行，不真正执行 Agent")
@click.option("--cleanup/--no-cleanup", default=True, help="兼容旧参数，内置执行器忽略此选项")
@click.option(
    "--shell",
    type=click.Choice(["auto", "pwsh", "powershell", "bash", "zsh"], case_sensitive=False),
    default="auto",
    help="dispatch 模式下指定使用的 Shell",
)
@click.option(
    "--executor",
    type=click.Choice(["auto", "dispatch", "builtin"], case_sensitive=False),
    default="auto",
    help="执行器类型：自动选择、外部 dispatch、或内置 Codex 执行器",
)
@click.option("--auto-commit/--no-auto-commit", default=True, help="内置执行器成功后自动提交当前任务")
def run(
    project: str | None,
    once: bool,
    limit: int,
    dry_run: bool,
    cleanup: bool,
    shell: str,
    executor: str,
    auto_commit: bool,
):
    """执行已注册项目队列中的任务."""
    if not project:
        echo("[red]错误: 必须指定 --project[/red]")
        return

    try:
        stats = run_backlog(
            project,
            once=once,
            limit=limit,
            dry_run=dry_run,
            cleanup=cleanup,
            shell=shell,
            executor=executor,
            auto_commit=auto_commit,
        )
    except RuntimeError as exc:
        echo(f"[red]{safe(exc)}[/red]")
        return

    echo(
        f"\n[dim]Run 完成: processed={stats['processed']} done={stats['done']} "
        f"failed={stats['failed']} requeued={stats['requeued']} cancelled={stats['cancelled']}[/dim]"
    )
