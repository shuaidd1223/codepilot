"""Run queued tasks via external dispatch or a built-in executor.

Shell/command helpers live in `run_shell.py`; git operations live in
`run_git.py`. Both are re-exported here so existing imports continue to work.
"""

from __future__ import annotations

import os
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
    _BuiltinLoopOutcome,
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
    _agent_label_runner,
    _is_builtin_agent_tooling_failure,
    _expected_phase_agent_label,
    _select_builtin_phase_fallback_agent,
    _run_builtin_phase,
    _extract_reviewer_findings,
    _make_phase_output_path,
    _run_phase_with_tooling_fallback,
    _run_builder_round,
    _run_reviewer_round,
    _finalize_executor_success,
    _run_builtin_round_loop,
    _map_builtin_loop_outcome,
    _run_builtin_executor,
)
from codepilot.commands.run_failure_triage import (  # noqa: F401
    _DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
    _DETERMINISTIC_FAILURE_TRIAGE_SCHEMA,
    _REVIEW_FAILURE_TRIAGE_SCHEMA,
    _append_task_content,
    _build_deterministic_failure_triage_prompt,
    _format_triage_merge_note,
    _trim_triage_text,
    _iter_triage_candidates as _iter_triage_candidates_impl,
    _resolve_triage_project_context as _resolve_triage_project_context_impl,
    apply_deterministic_failure_triage as _apply_deterministic_failure_triage_impl,
    apply_review_failure_triage as _apply_review_failure_triage_impl,
    triage_deterministic_failure as _triage_deterministic_failure_impl,
    triage_review_failure as _triage_review_failure_impl,
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
    should_stop = bool(stop_on_failure)
    return updated, should_stop


def _iter_triage_candidates(project: str, *, exclude_task_id: int) -> list[dict]:
    return _iter_triage_candidates_impl(
        project,
        exclude_task_id=exclude_task_id,
        db_module=db,
    )


def _resolve_triage_project_context(task: dict) -> tuple[str, str, object | None]:
    return _resolve_triage_project_context_impl(
        task,
        db_module=db,
        resolve_project_config_reference_fn=resolve_project_config_reference,
        load_project_config_fn=load_project_config,
    )


def _triage_deterministic_failure(task: dict, error_message: str) -> dict | None:
    return _triage_deterministic_failure_impl(
        task,
        error_message,
        db_module=db,
        resolve_project_config_reference_fn=resolve_project_config_reference,
        load_project_config_fn=load_project_config,
        resolve_planner_fn=resolve_planner,
        call_structured_fn=call_structured,
        gateway_request_cls=GatewayRequest,
        candidate_limit=_DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
    )


def _apply_deterministic_failure_triage(task: dict, error_message: str) -> str:
    return _apply_deterministic_failure_triage_impl(
        task,
        error_message,
        triage_fn=_triage_deterministic_failure,
    )


def _triage_review_failure(
    task: dict,
    error_message: str,
    *,
    review_output: str = "",
    output: str = "",
) -> dict | None:
    return _triage_review_failure_impl(
        task,
        error_message,
        review_output=review_output,
        builder_output=output,
        db_module=db,
        resolve_project_config_reference_fn=resolve_project_config_reference,
        load_project_config_fn=load_project_config,
        resolve_planner_fn=resolve_planner,
        call_structured_fn=call_structured,
        gateway_request_cls=GatewayRequest,
        candidate_limit=_DETERMINISTIC_FAILURE_TRIAGE_CANDIDATE_LIMIT,
    )


def _apply_review_failure_triage(
    task: dict,
    error_message: str,
    *,
    review_output: str = "",
    output: str = "",
    retry_on_failure: bool = False,
    stop_on_failure: bool = True,
) -> dict:
    return _apply_review_failure_triage_impl(
        task,
        error_message,
        review_output=review_output,
        builder_output=output,
        triage_fn=_triage_review_failure,
        mark_task_failed_fn=_mark_task_failed,
        handle_failure_fn=_handle_failure,
        db_module=db,
        retry_on_failure=retry_on_failure,
        stop_on_failure=stop_on_failure,
    )


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


def _finalize_failed_task_workspace(
    *,
    task_id: int,
    project_path: Path | str | None,
    worktree_path: Path | str | None,
    task_branch: str | None,
    base_branch: str | None,
    abandon_dirty_branch: bool = False,
) -> None:
    """End-of-task housekeeping for failure / cancellation paths.

    Goal: leave the repo in a state where the next ``run`` is unblocked even
    when no human is around. Performs four best-effort steps:

    1. Kill long-lived processes still running inside the worktree
       (delegates to :func:`_cleanup_worktree_leftovers`).
    2. **Worktree mode** (worktree_path != project_path): remove the task
       worktree directory + delete the task branch via
       ``_git_cleanup_task_worktree``.
    3. Pull the main repo back to ``base_branch`` (only when the working
       tree is clean) so the dashboard view is on base between tasks.
    4. **Branch mode** (worktree_path == project_path): the previous run
       just left a local task branch behind in the main repo. Now that
       HEAD is back on base, force-delete that branch so the next
       ``_git_prepare_task_branch`` call doesn't trip on "branch already
       exists".

    When ``abandon_dirty_branch`` is true, a dirty checked-out task branch is
    treated as an explicit terminal decision (discard / replan / merge into
    another task): reset and clean that task branch before switching back to
    base. Retry paths leave the branch intact so the next attempt can continue
    from the builder's partial work.

    Every step swallows its own exceptions and surfaces a yellow warning
    so a cleanup hiccup can never mask the real failure cause.
    """
    _cleanup_worktree_leftovers(worktree_path, project_path, task_id=task_id)

    if not project_path:
        return
    try:
        pp = Path(project_path)
    except Exception:
        return
    if not _git_is_repo(pp):
        return

    branch = (task_branch or "").strip()
    wt = None
    try:
        if worktree_path:
            wt = Path(worktree_path).resolve()
    except Exception:
        wt = None

    same_as_main = wt is not None and wt == pp.resolve()
    # worktree 模式：删独立 worktree（顺带删任务分支，git worktree remove
    # 之后 -D branch）。branch 模式（任务直接在主仓里切分支）这一步不会
    # 命中，留给下面"切回 base + 删孤儿分支"两步处理。
    if branch and not same_as_main and worktree_path:
        try:
            _git_cleanup_task_worktree(
                pp,
                worktree_path=worktree_path,
                task_branch=branch,
                keep_branch=False,
            )
            echo(f"[dim]任务 #{task_id} worktree 与分支 {branch} 已自动清理[/dim]")
        except Exception as exc:
            echo(
                f"[yellow]任务 #{task_id} worktree/分支自动清理失败: {safe(exc)}[/yellow]"
            )

    base = (base_branch or "").strip()
    if not base:
        return
    base_exists = False
    try:
        base_exists = _git_local_branch_exists(pp, base)
    except Exception as exc:
        echo(f"[yellow]检查 base_branch={base} 是否存在失败: {safe(exc)}[/yellow]")
        return
    if not base_exists:
        return

    # 切回 base 必须先于删任务分支，否则 HEAD 还指着任务分支，git branch -D
    # 会被 git 拒绝。只有在工作区干净时才切，避免 builder 半提交的代码被
    # 静默丢弃。current_after_checkout 用来下面的删分支守卫看 HEAD 是否
    # 真的回到了 base —— 如果没回到（脏工作区 / checkout 抛异常），删分支
    # 必须跳过，否则会撞上"git refused to delete the currently checked-out
    # branch" 留一堆 yellow warning + 分支照样残留。
    current_after_checkout = ""
    try:
        current = _git_current_branch(pp) or ""
        current_after_checkout = current
        if current and current != base and not _git_has_changes(pp):
            _git_checkout(pp, base)
            current_after_checkout = base
            echo(f"[dim]主仓库已切回 {base}[/dim]")
        elif (
            abandon_dirty_branch
            and branch
            and same_as_main
            and current == branch
            and _git_has_changes(pp)
        ):
            reset_code, reset_output = _run_command(["git", "reset", "--hard"], cwd=pp, timeout=120)
            clean_code, clean_output = _run_command(["git", "clean", "-fd"], cwd=pp, timeout=120)
            if reset_code != 0 or clean_code != 0:
                echo(
                    f"[yellow]任务 #{task_id} 分支 {branch} 终态清理失败: "
                    f"{safe((reset_output or clean_output)[:300])}[/yellow]"
                )
                return
            _git_checkout(pp, base)
            current_after_checkout = base
            echo(f"[dim]任务 #{task_id} 分支 {branch} 的未合并改动已按终态决策丢弃，主仓库已切回 {base}[/dim]")
    except Exception as exc:
        echo(f"[yellow]切回 base_branch={base} 失败: {safe(exc)}[/yellow]")
        return

    # branch 模式（同主仓 worktree）下，任务分支仍残留在本地分支列表里 —
    # 必须显式删掉，否则下一轮 _git_prepare_task_branch 会因 "branch already
    # exists / used by worktree" 卡住。worktree 模式上面已经 -D 过了，这里
    # 保险性二次检查；不会重复删。
    if branch and branch != base and same_as_main:
        # 关键守卫：HEAD 必须真的已经在 base 才能 -D。否则（脏工作区跳过了
        # checkout / checkout 失败）当前分支就是任务分支，git 会拒删。
        # retry 路径显式留下分支；terminal 路径可通过 abandon_dirty_branch
        # 在上面先丢弃改动再回 base。
        if current_after_checkout != base:
            echo(
                f"[yellow]任务 #{task_id} 分支 {branch} 暂未清理：HEAD 仍在 "
                f"{current_after_checkout or '(未知)'}（工作区可能有未提交改动），"
                "请手工 `git switch <base> && git branch -D <branch>`。[/yellow]"
            )
            return
        try:
            if _git_local_branch_exists(pp, branch):
                code, output = _run_command(
                    ["git", "branch", "-D", branch],
                    cwd=pp,
                    timeout=60,
                )
                if code == 0:
                    echo(f"[dim]任务分支 {branch} 已自动清理[/dim]")
                else:
                    echo(
                        f"[yellow]删除任务分支 {branch} 失败 (exit={code}): "
                        f"{safe(output[:200])}[/yellow]"
                    )
        except Exception as exc:
            echo(f"[yellow]删除任务分支 {branch} 异常: {safe(exc)}[/yellow]")


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
    click.echo(f"  查看完整日志: codepilot task logs {task_id} --full")


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

    from codepilot.cli_progress import maybe_cli_renderer

    try:
        with maybe_cli_renderer():
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
