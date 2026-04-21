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
from codepilot.config import load_project_config, resolve_planner
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

STATUS_CONSOLE = Console()




def _project_config(project_ref: str | dict | None):
    if isinstance(project_ref, dict):
        return load_project_config(project_ref.get("path"), config_file=project_ref.get("config_file"))
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
    log_path = runtime_dir / f"task-{Path(task_file).stem}-dispatch.log"
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

    config_ref = config_file or project_path
    config = (
        load_project_config(project_path or "", config_file=config_file or None)
        if config_ref
        else None
    )
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
    db.init_db()
    proj = db.get_project(project)
    if not proj:
        raise RuntimeError(f"项目 '{project}' 未注册，请先运行 codepilot init")

    reaped = reap_stalled_tasks(project)
    for task in reaped:
        echo(f"[yellow]已回收卡住任务 #{task['id']}：{task['title']}[/yellow]")

    live_tasks = list_live_tasks(project)
    if live_tasks:
        current = live_tasks[0]
        echo(
            f"[yellow]项目当前已有运行中的任务 #{current['id']}，阶段={current.get('run_phase') or '-'}，"
            f"pid={current.get('active_pid') or '-'}。本次不再启动新任务。[/yellow]"
        )
        return {"processed": 0, "done": 0, "failed": 0, "requeued": 0, "cancelled": 0, "executor": executor}

    project_path = Path(proj["path"]).resolve()
    config = _project_config(proj)
    base_branch = _resolve_project_base_branch(proj, config)
    preferred_shell = shell if shell != "auto" else ((config.shell.preferred if config else "") or "")
    shell_info = detect_best_shell(preferred_shell if preferred_shell != "auto" else None)

    dispatch_path = _find_dispatch_script(str(project_path))
    resolved_executor = executor
    if resolved_executor == "auto":
        resolved_executor = "dispatch" if dispatch_path else "builtin"

    max_review_rounds = 2
    if config and getattr(config, "automation", None):
        max_review_rounds = int(getattr(config.automation, "max_review_rounds", 2) or 2)
    max_review_rounds = max(1, min(max_review_rounds, 5))

    echo(f"[dim]使用执行器: {resolved_executor}[/dim]")
    if resolved_executor == "dispatch":
        echo(f"[dim]使用 Shell: {shell_info.version_hint}[/dim]")
    if not quiet:
        render_project_dashboard(project, include_done=False, max_rows=10, title="执行队列")

    stats = {
        "processed": 0,
        "done": 0,
        "failed": 0,
        "requeued": 0,
        "cancelled": 0,
        "executor": resolved_executor,
    }

    for _ in range(limit):
        tasks = db.next_backlog_task(project)
        if not tasks:
            echo("[yellow]没有待执行的任务[/yellow]")
            break

        task = tasks[0]
        task_id = task["id"]
        preflight_error = ""
        if resolved_executor == "builtin":
            effective_agent_mode = (
                "codex"
                if _builtin_review_requires_git(task.get("agent", "codex"), task=task, project_ref=proj)
                else "dual"
            )
            preflight_error = _builtin_preflight_error(project_path, auto_commit, effective_agent_mode)
        task_branch = _git_current_branch(project_path)
        execution_path = project_path
        # builtin 默认在独立 worktree 中执行；也支持 direct/branch 两种
        # 主工作区执行方式。
        per_task_branch_enabled = (
            resolved_executor == "builtin"
            and bool(getattr(getattr(config, "automation", None), "per_task_branch", True))
        )
        task_workspace = str(getattr(getattr(config, "automation", None), "task_workspace", "branch") or "branch").strip().lower()
        if task_workspace not in {"direct", "branch", "worktree"}:
            task_workspace = "branch"
        if per_task_branch_enabled and not preflight_error and not dry_run:
            try:
                if task_workspace == "worktree":
                    lock_error = _builtin_base_branch_lock_error(project_path, base_branch)
                    if lock_error:
                        raise RuntimeError(lock_error)
                    prepared_branch, prepared_worktree = _git_prepare_task_worktree(
                        project_path,
                        task_id=task_id,
                        title=task["title"],
                        base_branch=base_branch,
                        worktree_path=_task_worktree_path(proj, task_id=task_id, title=task["title"], config=config),
                    )
                    if prepared_branch:
                        task_branch = prepared_branch
                    execution_path = prepared_worktree
                elif task_workspace == "branch":
                    prepared_branch = _git_prepare_task_branch(
                        project_path,
                        task_id=task_id,
                        title=task["title"],
                        base_branch=base_branch,
                    )
                    if prepared_branch:
                        task_branch = prepared_branch
                    execution_path = project_path
                else:
                    if _git_is_repo(project_path) and _git_local_branch_exists(project_path, base_branch):
                        _git_checkout(project_path, base_branch)
                    task_branch = _git_current_branch(project_path)
                    execution_path = project_path
            except Exception as exc:
                preflight_error = str(exc)
        if preflight_error:
            if retry_on_failure:
                db.update_task(task_id, status="backlog", error_message=preflight_error)
                echo(f"[yellow]{preflight_error}[/yellow]")
                click.echo(f"  处理: 任务 #{task_id} 保持 backlog，等待你修正环境后再执行")
            else:
                _mark_task_failed(task, preflight_error)
                echo(f"[red]{preflight_error}[/red]")
                click.echo(f"  处理: 任务 #{task_id} 已直接标记 failed，不再自动回退")
                stats["failed"] += 1
            stats["processed"] += 1
            if retry_on_failure:
                stats["requeued"] += 1
            if not quiet:
                render_project_dashboard(project, include_done=False, max_rows=10, title="当前任务面板")
            break

        task_file = _pick_task_file(
            project_path,
            task_id,
            tracked=(resolved_executor == "dispatch"),
            project=proj,
        )
        task_file.write_text(_build_task_md(task), encoding="utf-8")

        db.update_task(
            task_id,
            status="in_progress",
            started_at=datetime.now().isoformat(),
            branch_name=task_branch,
            worktree_path=str(execution_path),
            stop_requested=0,
            stop_reason=None,
            run_phase="pending",
            heartbeat_at=datetime.now().isoformat(),
            active_pid=None,
            current_log_path=None,
            last_output="",
            # Clear any previous preflight-skip warning / stale failure
            # banner so the UI stops showing it the moment this run starts.
            error_message="",
        )

        echo(f"[cyan]-> 执行任务 #{task_id}[/cyan]  {task['title']}")
        if limit > 0:
            click.echo(f"  进度: {stats['processed'] + 1}/{limit}")
        click.echo(f"  Agent: {task['agent']}  优先级: {task['priority']}")
        click.echo(f"  任务文件: {task_file}")

        if dry_run:
            clear_task_runtime(task_id, status="backlog", started_at=None, stop_requested=0, stop_reason=None)
            echo("[dim]  [DryRun 模式，跳过实际执行][/dim]")
            click.echo()
            stats["processed"] += 1
            if once:
                break
            continue

        try:
            if resolved_executor == "dispatch":
                started_at = datetime.now()
                exit_code, output = _run_dispatch(proj, task_file, task["agent"], shell_info, dry_run=dry_run)
                _write_task_log(task_id, task["agent"], "dispatch", output, exit_code, started_at)
                result = ExecutionResult(exit_code=exit_code, output=output, executor="dispatch")
            else:
                result = _run_builtin_executor(
                    task,
                    proj,
                    task_file,
                    auto_commit=auto_commit,
                    max_review_rounds=max_review_rounds,
                    execution_path=execution_path,
                )
        except TaskCancelled as exc:
            clear_task_runtime(
                task_id,
                status="cancelled",
                completed_at=datetime.now().isoformat(),
                error_message=str(exc),
                delivery_record="",
                stop_requested=0,
                stop_reason=None,
            )
            _cleanup_worktree_leftovers(execution_path, project_path, task_id=task_id)
            echo(f"[yellow]任务 #{task_id} 已停止[/yellow]")
            notify_task_status(str(project_path), task_id, task["title"], "cancelled", str(exc))
            stats["cancelled"] += 1
            stats["processed"] += 1
            if once:
                break
            continue
        except PreflightSkipError as exc:
            # Preflight 级别的"跳过但不扣重试次数"：推回 backlog，清理运行态，
            # 留下错误信息让人类/下一轮 daemon 能看到。不走 _handle_failure。
            skip_message = str(exc)
            clear_task_runtime(
                task_id,
                status="backlog",
                started_at=None,
                error_message=skip_message[:4000],
                stop_requested=0,
                stop_reason=None,
            )
            echo(f"[yellow]任务 #{task_id} 预检跳过（不扣重试次数）：{skip_message.splitlines()[0]}[/yellow]")
            stats["requeued"] += 1
            stats["processed"] += 1
            if once:
                break
            continue
        except Exception as exc:
            error_text = str(exc)
            if retry_on_failure:
                updated, should_stop = _handle_failure(task, error_text, stop_on_failure=(resolved_executor == "builtin"))
            else:
                updated = _mark_task_failed(task, error_text)
                should_stop = True
            echo(f"[red]执行出错: {safe(exc)}[/red]")
            if updated["status"] == "failed":
                stats["failed"] += 1
                notify_task_status(str(project_path), task_id, task["title"], "failed", error_text)
            else:
                stats["requeued"] += 1
            _show_failure_feedback(
                task_id,
                title=task["title"],
                error_message=error_text,
                requeued=updated["status"] != "failed",
            )
            stats["processed"] += 1
            if not quiet:
                render_project_dashboard(project, include_done=False, max_rows=10, title="当前任务面板")
            if should_stop or once:
                break
            continue

        if result.exit_code == 0 and per_task_branch_enabled and task_workspace in {"branch", "worktree"}:
            try:
                if task_workspace == "worktree":
                    merge_summary = _git_merge_task_worktree(
                        project_path,
                        task_id=task_id,
                        title=task["title"],
                        task_branch=task_branch,
                        base_branch=base_branch,
                        worktree_path=execution_path,
                    )
                else:
                    merge_summary = _git_merge_task_branch(
                        project_path,
                        task_id=task_id,
                        title=task["title"],
                        task_branch=task_branch,
                        base_branch=base_branch,
                    )
                if merge_summary:
                    result.summary = " | ".join(part for part in [result.summary, merge_summary] if part)
            except Exception as exc:
                result = ExecutionResult(
                    exit_code=2,
                    output=result.output,
                    review_output=result.review_output,
                    summary=f"任务执行完成但回合并失败: {exc}",
                    executor=result.executor,
                )

        if result.exit_code == 0:
            clear_task_runtime(
                task_id,
                status="done",
                completed_at=datetime.now().isoformat(),
                error_message="",
                delivery_record=result.summary or result.review_output or result.output,
                stop_requested=0,
                stop_reason=None,
            )
            _cleanup_worktree_leftovers(execution_path, project_path, task_id=task_id)
            echo(f"[green][OK] 任务 #{task_id} 完成[/green]")
            notify_task_status(str(project_path), task_id, task["title"], "done")
            stats["done"] += 1
        else:
            error_message = result.summary or result.review_output or result.output or f"执行失败 (exit={result.exit_code})"
            # 确定性失败 (如 reviewer 连 N 轮 FAIL) 不走任务级 retry，直接标 failed 避免重复烧 token
            if result.deterministic_failure:
                error_message = _apply_deterministic_failure_triage(task, error_message)
                updated = _mark_task_failed(task, error_message)
                should_stop = (resolved_executor == "builtin")
            elif retry_on_failure:
                updated, should_stop = _handle_failure(task, error_message, stop_on_failure=(resolved_executor == "builtin"))
            else:
                updated = _mark_task_failed(task, error_message)
                should_stop = True
            _cleanup_worktree_leftovers(execution_path, project_path, task_id=task_id)
            if updated["status"] == "failed":
                stats["failed"] += 1
                notify_task_status(str(project_path), task_id, task["title"], "failed", error_message)
            else:
                stats["requeued"] += 1
            _show_failure_feedback(
                task_id,
                title=task["title"],
                error_message=error_message,
                review_output=result.review_output,
                output=result.output,
                requeued=updated["status"] != "failed",
            )
            if should_stop:
                stats["processed"] += 1
                if not quiet:
                    render_project_dashboard(project, include_done=False, max_rows=10, title="当前任务面板")
                break

        if result.output:
            summary_lines = _summarize_output(result.output)
            if summary_lines:
                echo("[dim]--- builder 摘要 ---[/dim]")
                for line in summary_lines:
                    click.echo(f"  {line}")
        if result.review_output:
            # Show review verdict concisely
            review_lines = [l.strip() for l in result.review_output.splitlines()
                           if l.strip() and any(kw in l for kw in ("VERDICT", "pass", "fail", "PASS", "FAIL", "[P", "Restore", "Fix", "issue", "regression"))]
            if not review_lines:
                review_lines = [l.strip() for l in result.review_output.splitlines() if l.strip()][-3:]
            if review_lines:
                echo("[dim]--- reviewer 摘要 ---[/dim]")
                for line in review_lines[:5]:
                    click.echo(f"  {line[:120]}")
        click.echo()

        stats["processed"] += 1
        render_project_dashboard(project, include_done=False, max_rows=10, title="当前任务面板")
        if once:
            break
        if resolved_executor == "dispatch":
            time.sleep(2)

    return stats


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
