"""Queue orchestration for ``codepilot run``.

This module keeps the run loop focused on orchestration boundaries:
context resolution, per-task workspace preparation, executor dispatch, and
result finalization. Concrete execution details remain in ``run.py`` /
``run_builtin.py`` and are late-bound for test monkeypatch compatibility.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import click

from codepilot import db


def _runner_module():
    """Late-bind through ``codepilot.commands.run`` for legacy monkeypatches."""
    from codepilot.commands import run as _run_mod

    return _run_mod


@dataclass
class _RunContext:
    project: dict
    project_path: Path
    config: object | None
    base_branch: str
    shell_info: object
    executor: str
    max_review_rounds: int
    per_task_branch_enabled: bool
    task_workspace: str


@dataclass
class _TaskWorkspacePlan:
    task_branch: str
    execution_path: Path
    preflight_error: str = ""


def _resolve_run_context(project_record: dict, *, shell: str, executor: str) -> _RunContext:
    runner = _runner_module()
    project_path = Path(project_record["path"]).resolve()
    config = runner._project_config(project_record)
    base_branch = runner._resolve_project_base_branch(project_record, config)

    preferred_shell = shell if shell != "auto" else ((config.shell.preferred if config else "") or "")
    shell_info = runner.detect_best_shell(preferred_shell if preferred_shell != "auto" else None)

    dispatch_path = runner._find_dispatch_script(str(project_path))
    resolved_executor = executor
    if resolved_executor == "auto":
        resolved_executor = "dispatch" if dispatch_path else "builtin"

    max_review_rounds = 2
    if config and getattr(config, "automation", None):
        max_review_rounds = int(getattr(config.automation, "max_review_rounds", 2) or 2)
    max_review_rounds = max(1, min(max_review_rounds, 5))

    per_task_branch_enabled = (
        resolved_executor == "builtin"
        and bool(getattr(getattr(config, "automation", None), "per_task_branch", True))
    )
    task_workspace = str(getattr(getattr(config, "automation", None), "task_workspace", "branch") or "branch").strip().lower()
    if task_workspace not in {"direct", "branch", "worktree"}:
        task_workspace = "branch"

    return _RunContext(
        project=project_record,
        project_path=project_path,
        config=config,
        base_branch=base_branch,
        shell_info=shell_info,
        executor=resolved_executor,
        max_review_rounds=max_review_rounds,
        per_task_branch_enabled=per_task_branch_enabled,
        task_workspace=task_workspace,
    )


def _prepare_task_workspace(
    context: _RunContext,
    task: dict,
    *,
    auto_commit: bool,
    dry_run: bool,
) -> _TaskWorkspacePlan:
    runner = _runner_module()

    preflight_error = ""
    if context.executor == "builtin":
        effective_agent_mode = (
            "codex"
            if runner._builtin_review_requires_git(task.get("agent", "codex"), task=task, project_ref=context.project)
            else "dual"
        )
        preflight_error = runner._builtin_preflight_error(context.project_path, auto_commit, effective_agent_mode)

    task_branch = runner._git_current_branch(context.project_path)
    execution_path = context.project_path

    if context.per_task_branch_enabled and not preflight_error and not dry_run:
        try:
            if context.task_workspace == "worktree":
                lock_error = runner._builtin_base_branch_lock_error(context.project_path, context.base_branch)
                if lock_error:
                    raise RuntimeError(lock_error)
                prepared_branch, prepared_worktree = runner._git_prepare_task_worktree(
                    context.project_path,
                    task_id=task["id"],
                    title=task["title"],
                    base_branch=context.base_branch,
                    worktree_path=runner._task_worktree_path(
                        context.project,
                        task_id=task["id"],
                        title=task["title"],
                        config=context.config,
                    ),
                )
                if prepared_branch:
                    task_branch = prepared_branch
                execution_path = prepared_worktree
            elif context.task_workspace == "branch":
                prepared_branch = runner._git_prepare_task_branch(
                    context.project_path,
                    task_id=task["id"],
                    title=task["title"],
                    base_branch=context.base_branch,
                )
                if prepared_branch:
                    task_branch = prepared_branch
                execution_path = context.project_path
            else:
                if runner._git_is_repo(context.project_path) and runner._git_local_branch_exists(
                    context.project_path, context.base_branch
                ):
                    runner._git_checkout(context.project_path, context.base_branch)
                task_branch = runner._git_current_branch(context.project_path)
                execution_path = context.project_path
        except Exception as exc:
            preflight_error = str(exc)

    return _TaskWorkspacePlan(
        task_branch=task_branch,
        execution_path=execution_path,
        preflight_error=preflight_error,
    )


def _mark_task_started(
    context: _RunContext,
    task: dict,
    task_file: Path,
    workspace: _TaskWorkspacePlan,
    *,
    progress_text: str = "",
) -> None:
    db.update_task(
        task["id"],
        status="in_progress",
        started_at=datetime.now().isoformat(),
        branch_name=workspace.task_branch,
        worktree_path=str(workspace.execution_path),
        stop_requested=0,
        stop_reason=None,
        run_phase="pending",
        heartbeat_at=datetime.now().isoformat(),
        active_pid=None,
        current_log_path=None,
        last_output="",
        error_message="",
    )

    runner = _runner_module()
    runner.echo(f"[cyan]-> 执行任务 #{task['id']}[/cyan]  {task['title']}")
    if progress_text:
        click.echo(f"  进度: {progress_text}")
    click.echo(f"  Agent: {task['agent']}  优先级: {task['priority']}")
    click.echo(f"  任务文件: {task_file}")


def _execute_task(
    context: _RunContext,
    task: dict,
    task_file: Path,
    *,
    auto_commit: bool,
    execution_path: Path,
):
    runner = _runner_module()
    if context.executor == "dispatch":
        started_at = datetime.now()
        exit_code, output = runner._run_dispatch(context.project, task_file, task["agent"], context.shell_info, dry_run=False)
        runner._write_task_log(task["id"], task["agent"], "dispatch", output, exit_code, started_at)
        return runner.ExecutionResult(exit_code=exit_code, output=output, executor="dispatch")

    return runner._run_builtin_executor(
        task,
        context.project,
        task_file,
        auto_commit=auto_commit,
        max_review_rounds=context.max_review_rounds,
        execution_path=execution_path,
    )


def _maybe_merge_task_branch(
    context: _RunContext,
    task: dict,
    workspace: _TaskWorkspacePlan,
    result,
):
    runner = _runner_module()
    if result.exit_code != 0:
        return result
    if not context.per_task_branch_enabled:
        return result
    if context.task_workspace not in {"branch", "worktree"}:
        return result

    try:
        if context.task_workspace == "worktree":
            merge_summary = runner._git_merge_task_worktree(
                context.project_path,
                task_id=task["id"],
                title=task["title"],
                task_branch=workspace.task_branch,
                base_branch=context.base_branch,
                worktree_path=workspace.execution_path,
            )
        else:
            merge_summary = runner._git_merge_task_branch(
                context.project_path,
                task_id=task["id"],
                title=task["title"],
                task_branch=workspace.task_branch,
                base_branch=context.base_branch,
            )
        if merge_summary:
            result.summary = " | ".join(part for part in [result.summary, merge_summary] if part)
        return result
    except Exception as exc:
        return runner.ExecutionResult(
            exit_code=2,
            output=result.output,
            review_output=result.review_output,
            summary=f"任务执行完成但回合并失败: {exc}",
            executor=result.executor,
        )


def _emit_phase_summaries(result) -> None:
    runner = _runner_module()
    if result.output:
        summary_lines = runner._summarize_output(result.output)
        if summary_lines:
            runner.echo("[dim]--- builder 摘要 ---[/dim]")
            for line in summary_lines:
                click.echo(f"  {line}")

    if result.review_output:
        review_lines = [
            line.strip()
            for line in result.review_output.splitlines()
            if line.strip()
            and any(kw in line for kw in ("VERDICT", "pass", "fail", "PASS", "FAIL", "[P", "Restore", "Fix", "issue", "regression"))
        ]
        if not review_lines:
            review_lines = [line.strip() for line in result.review_output.splitlines() if line.strip()][-3:]
        if review_lines:
            runner.echo("[dim]--- reviewer 摘要 ---[/dim]")
            for line in review_lines[:5]:
                click.echo(f"  {line[:120]}")
    click.echo()


def _render_dashboard(project: str, *, quiet: bool, title: str) -> None:
    if quiet:
        return
    _runner_module().render_project_dashboard(project, include_done=False, max_rows=10, title=title)


def _handle_workspace_preflight_error(
    *,
    context: _RunContext,
    task: dict,
    preflight_error: str,
    retry_on_failure: bool,
    stats: dict,
    project: str,
    quiet: bool,
) -> bool:
    """Handle workspace preparation failures before the executor starts.

    Returns whether the outer loop should stop for this run.
    """
    runner = _runner_module()
    task_id = task["id"]
    if retry_on_failure:
        db.update_task(task_id, status="backlog", error_message=preflight_error)
        runner.echo(f"[yellow]{preflight_error}[/yellow]")
        click.echo(f"  处理: 任务 #{task_id} 保持 backlog，等待你修正环境后再执行")
        stats["requeued"] += 1
    else:
        runner._mark_task_failed(task, preflight_error)
        runner.echo(f"[red]{preflight_error}[/red]")
        click.echo(f"  处理: 任务 #{task_id} 已直接标记 failed，不再自动回退")
        stats["failed"] += 1

    stats["processed"] += 1
    _render_dashboard(project, quiet=quiet, title="当前任务面板")
    return True


def _handle_executor_cancelled(
    *,
    context: _RunContext,
    task: dict,
    task_id: int,
    workspace: _TaskWorkspacePlan,
    exc: Exception,
    stats: dict,
    once: bool,
) -> bool:
    """Finalize task state after receiving a cancellation signal."""
    runner = _runner_module()
    runner.clear_task_runtime(
        task_id,
        status="cancelled",
        completed_at=datetime.now().isoformat(),
        error_message=str(exc),
        delivery_record="",
        stop_requested=0,
        stop_reason=None,
    )
    runner._cleanup_worktree_leftovers(workspace.execution_path, context.project_path, task_id=task_id)
    runner.echo(f"[yellow]任务 #{task_id} 已停止[/yellow]")
    runner.notify_task_status(str(context.project_path), task_id, task["title"], "cancelled", str(exc))
    stats["cancelled"] += 1
    stats["processed"] += 1
    return once


def _handle_executor_preflight_skip(
    *,
    task_id: int,
    skip_message: str,
    stats: dict,
    once: bool,
) -> bool:
    """Requeue task when builtin executor asks to skip without consuming retry."""
    runner = _runner_module()
    runner.clear_task_runtime(
        task_id,
        status="backlog",
        started_at=None,
        error_message=skip_message[:4000],
        stop_requested=0,
        stop_reason=None,
    )
    runner.echo(f"[yellow]任务 #{task_id} 预检跳过（不扣重试次数）：{skip_message.splitlines()[0]}[/yellow]")
    stats["requeued"] += 1
    stats["processed"] += 1
    return once


def _handle_executor_exception(
    *,
    context: _RunContext,
    task: dict,
    task_id: int,
    error_text: str,
    retry_on_failure: bool,
    project: str,
    quiet: bool,
    once: bool,
    stats: dict,
) -> bool:
    """Finalize task state after unexpected executor exceptions.

    Returns whether the outer loop should stop.
    """
    runner = _runner_module()
    if retry_on_failure:
        updated, should_stop = runner._handle_failure(task, error_text, stop_on_failure=(context.executor == "builtin"))
    else:
        updated = runner._mark_task_failed(task, error_text)
        should_stop = True
    runner.echo(f"[red]执行出错: {runner.safe(error_text)}[/red]")
    if updated["status"] == "failed":
        stats["failed"] += 1
        runner.notify_task_status(str(context.project_path), task_id, task["title"], "failed", error_text)
    else:
        stats["requeued"] += 1
    runner._show_failure_feedback(
        task_id,
        title=task["title"],
        error_message=error_text,
        requeued=updated["status"] != "failed",
    )
    stats["processed"] += 1
    _render_dashboard(project, quiet=quiet, title="当前任务面板")
    return should_stop or once


def _handle_execution_result(
    *,
    context: _RunContext,
    task: dict,
    task_id: int,
    workspace: _TaskWorkspacePlan,
    result,
    retry_on_failure: bool,
    project: str,
    quiet: bool,
    once: bool,
    stats: dict,
) -> bool:
    """Finalize a normal execution result and return loop stop decision."""
    runner = _runner_module()

    if result.exit_code == 0:
        runner.clear_task_runtime(
            task_id,
            status="done",
            completed_at=datetime.now().isoformat(),
            error_message="",
            delivery_record=result.summary or result.review_output or result.output,
            stop_requested=0,
            stop_reason=None,
        )
        runner._cleanup_worktree_leftovers(workspace.execution_path, context.project_path, task_id=task_id)
        runner.echo(f"[green][OK] 任务 #{task_id} 完成[/green]")
        runner.notify_task_status(str(context.project_path), task_id, task["title"], "done")
        stats["done"] += 1
    else:
        error_message = result.summary or result.review_output or result.output or f"执行失败 (exit={result.exit_code})"
        if result.deterministic_failure:
            error_message = runner._apply_deterministic_failure_triage(task, error_message)
            updated = runner._mark_task_failed(task, error_message)
            should_stop = context.executor == "builtin"
        elif retry_on_failure:
            updated, should_stop = runner._handle_failure(task, error_message, stop_on_failure=(context.executor == "builtin"))
        else:
            updated = runner._mark_task_failed(task, error_message)
            should_stop = True
        runner._cleanup_worktree_leftovers(workspace.execution_path, context.project_path, task_id=task_id)
        if updated["status"] == "failed":
            stats["failed"] += 1
            runner.notify_task_status(str(context.project_path), task_id, task["title"], "failed", error_message)
        else:
            stats["requeued"] += 1
        runner._show_failure_feedback(
            task_id,
            title=task["title"],
            error_message=error_message,
            review_output=result.review_output,
            output=result.output,
            requeued=updated["status"] != "failed",
        )
        if should_stop:
            stats["processed"] += 1
            _render_dashboard(project, quiet=quiet, title="当前任务面板")
            return True

    _emit_phase_summaries(result)
    stats["processed"] += 1
    _render_dashboard(project, quiet=quiet, title="当前任务面板")
    return once


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
    """Execute up to ``limit`` runnable tasks for a project."""
    runner = _runner_module()

    db.init_db()
    project_record = db.get_project(project)
    if not project_record:
        raise RuntimeError(f"项目 '{project}' 未注册，请先运行 codepilot init")

    reaped = runner.reap_stalled_tasks(project)
    for task in reaped:
        runner.echo(f"[yellow]已回收卡住任务 #{task['id']}：{task['title']}[/yellow]")

    live_tasks = runner.list_live_tasks(project)
    if live_tasks:
        current = live_tasks[0]
        runner.echo(
            f"[yellow]项目当前已有运行中的任务 #{current['id']}，阶段={current.get('run_phase') or '-'}，"
            f"pid={current.get('active_pid') or '-'}。本次不再启动新任务。[/yellow]"
        )
        return {"processed": 0, "done": 0, "failed": 0, "requeued": 0, "cancelled": 0, "executor": executor}

    context = _resolve_run_context(project_record, shell=shell, executor=executor)
    runner.echo(f"[dim]使用执行器: {context.executor}[/dim]")
    if context.executor == "dispatch":
        runner.echo(f"[dim]使用 Shell: {context.shell_info.version_hint}[/dim]")
    _render_dashboard(project, quiet=quiet, title="执行队列")

    stats = {
        "processed": 0,
        "done": 0,
        "failed": 0,
        "requeued": 0,
        "cancelled": 0,
        "executor": context.executor,
    }

    for _ in range(limit):
        tasks = db.next_backlog_task(project)
        if not tasks:
            runner.echo("[yellow]没有待执行的任务[/yellow]")
            break

        task = tasks[0]
        task_id = task["id"]
        workspace = _prepare_task_workspace(context, task, auto_commit=auto_commit, dry_run=dry_run)
        if workspace.preflight_error:
            if _handle_workspace_preflight_error(
                context=context,
                task=task,
                preflight_error=workspace.preflight_error,
                retry_on_failure=retry_on_failure,
                stats=stats,
                project=project,
                quiet=quiet,
            ):
                break

        task_file = runner._pick_task_file(
            context.project_path,
            task_id,
            tracked=(context.executor == "dispatch"),
            project=context.project,
        )
        task_file.write_text(runner._build_task_md(task), encoding="utf-8")

        progress_text = f"{stats['processed'] + 1}/{limit}" if limit > 0 else ""
        _mark_task_started(context, task, task_file, workspace, progress_text=progress_text)

        if dry_run:
            runner.clear_task_runtime(task_id, status="backlog", started_at=None, stop_requested=0, stop_reason=None)
            runner.echo("[dim]  [DryRun 模式，跳过实际执行][/dim]")
            click.echo()
            stats["processed"] += 1
            if once:
                break
            continue

        try:
            result = _execute_task(
                context,
                task,
                task_file,
                auto_commit=auto_commit,
                execution_path=workspace.execution_path,
            )
        except runner.TaskCancelled as exc:
            if _handle_executor_cancelled(
                context=context,
                task=task,
                task_id=task_id,
                workspace=workspace,
                exc=exc,
                stats=stats,
                once=once,
            ):
                break
            continue
        except runner.PreflightSkipError as exc:
            if _handle_executor_preflight_skip(
                task_id=task_id,
                skip_message=str(exc),
                stats=stats,
                once=once,
            ):
                break
            continue
        except Exception as exc:
            if _handle_executor_exception(
                context=context,
                task=task,
                task_id=task_id,
                error_text=str(exc),
                retry_on_failure=retry_on_failure,
                project=project,
                quiet=quiet,
                once=once,
                stats=stats,
            ):
                break
            continue

        result = _maybe_merge_task_branch(context, task, workspace, result)
        if _handle_execution_result(
            context=context,
            task=task,
            task_id=task_id,
            workspace=workspace,
            result=result,
            retry_on_failure=retry_on_failure,
            project=project,
            quiet=quiet,
            once=once,
            stats=stats,
        ):
            break
        if context.executor == "dispatch":
            time.sleep(2)

    return stats
