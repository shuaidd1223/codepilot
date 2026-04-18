"""Run queued tasks via external dispatch or a built-in executor.

Shell/command helpers live in `run_shell.py`; git operations live in
`run_git.py`. Both are re-exported here so existing imports continue to work.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
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
    resolve_cli_provider,
)
from codepilot.commands.status import _resolve_project, render_project_dashboard
from codepilot.config import load_project_config
from codepilot.output import echo, safe
from codepilot.runtime import (
    HEARTBEAT_INTERVAL_SECONDS,
    clear_task_runtime,
    get_stop_request,
    list_live_tasks,
    reap_stalled_tasks,
    stop_process_tree,
    tail_text,
    update_task_runtime,
)
from codepilot.webhook import notify_task_status

# Re-export shell + command helpers
from codepilot.commands.run_shell import (  # noqa: F401
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
    _git_checkout,
    _git_current_branch,
    _git_has_changes,
    _git_is_repo,
    _git_local_branch_exists,
    _git_merge_task_branch,
    _git_prepare_task_branch,
    _resolve_project_base_branch,
    _slugify_branch_part,
    _task_branch_name,
)

STATUS_CONSOLE = Console()


@dataclass
class ExecutionResult:
    """Normalized task execution result."""

    exit_code: int
    output: str = ""
    review_output: str = ""
    summary: str = ""
    executor: str = "dispatch"



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

    package_scripts = [
        Path(__file__).resolve().parent.parent / "scripts" / "task-dispatch.ps1",
        Path(__file__).resolve().parent.parent / "scripts" / "task-dispatch.sh",
    ]
    candidates.extend(package_scripts)
    candidates.append(Path.home() / ".codepilot" / "scripts" / "task-dispatch.ps1")
    candidates.append(Path.home() / ".codepilot" / "scripts" / "task-dispatch.sh")

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


def _pick_task_file(project_path: Path, task_id: int, tracked: bool = True) -> Path:
    backlog = (
        project_path / "tasks" / "backlog"
        if tracked
        else Path.home() / ".codepilot" / "task-files" / project_path.name
    )
    backlog.mkdir(parents=True, exist_ok=True)
    return backlog / f"{task_id:03d}-task.md"



def _builtin_runtime_dir(project: dict) -> Path:
    """Store builtin executor artifacts outside the repo to avoid polluting commits."""
    project_path = Path(project["path"]).resolve()
    project_name = project.get("name") or project_path.name or "project"
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in project_name).strip("-")
    safe_name = safe_name or "project"
    fingerprint = hashlib.sha1(str(project_path).encode("utf-8")).hexdigest()[:10]
    output_dir = Path.home() / ".codepilot" / "runs" / f"{safe_name}-{fingerprint}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _builtin_preflight_error(project_path: Path, auto_commit: bool, agent_mode: str = "codex") -> str:
    """Return a human-readable reason why builtin execution should not start yet."""
    review_requires_git = normalize_agent_name(agent_mode or "dual") == "codex"
    if review_requires_git and not _git_is_repo(project_path):
        return (
            "当前内置 reviewer（Codex review）需要 Git 仓库来审查未提交改动。"
            "当前项目还不是 Git 仓库，本次跳过执行且不消耗重试次数。"
            "请先执行 git init 并完成首次提交，或改用 dual / claude 再执行。"
        )
    if auto_commit and not _git_is_repo(project_path):
        return (
            "内置执行器默认会在成功后自动提交，但当前项目还不是 Git 仓库。"
            "为避免执行完成后才在提交阶段失败，本次跳过执行且不消耗重试次数。"
            "请先执行 git init 并完成首次提交，或改用 --no-auto-commit 再执行。"
        )
    if auto_commit and _git_has_changes(project_path):
        return (
            "内置执行器检测到当前工作区已有未提交改动。"
            "为避免把现有改动和任务结果混在同一次自动提交中，本次跳过执行且不消耗重试次数。"
            "请先提交/暂存现有改动，或改用 --no-auto-commit 再执行。"
        )
    return ""




def _write_task_log(task_id: int, agent: str, phase: str, output: str, exit_code: int, started_at: datetime) -> None:
    finished_at = datetime.now()
    db.create_task_log(
        task_id=task_id,
        agent=agent,
        phase=phase,
        output=output,
        exit_code=exit_code,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration=int((finished_at - started_at).total_seconds()),
    )


def _read_output_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _build_builtin_prompt(task: dict, task_file: Path) -> str:
    return "\n".join(
        [
            f"你正在执行排队任务 #{task['id']}：{task['title']}",
            "",
            "要求：",
            "1. 阅读任务文件并在当前仓库中直接完成实现。",
            "2. 必须自己运行必要的检查命令，并根据结果修正问题。",
            "3. 不要等待人工确认，不要进入交互模式。",
            "4. 结束时输出三段：Summary、Changed Files、Validation。",
            "",
            f"任务文件：{task_file}",
        ]
    )


def _build_review_prompt(task: dict) -> str:
    return "\n".join(
        [
            f"请审查当前仓库中为任务 #{task['id']} `{task['title']}` 产生的未提交改动。",
            "聚焦：功能正确性、回归风险、遗漏的验证。",
            "如果没有阻塞问题，最后单独输出一行 `VERDICT: PASS`。",
            "如果有阻塞问题，最后单独输出一行 `VERDICT: FAIL`，并先列出发现。",
        ]
    )


def _extract_review_verdict(review_output: str, reviewer_agent: str = "") -> str:
    verdict_pattern = re.compile(r"VERDICT\s*:\s*(PASS|FAIL)\b", re.IGNORECASE)
    for line in reversed(review_output.splitlines()):
        match = verdict_pattern.search(line)
        if match:
            return match.group(1).lower()

    normalized = reviewer_agent.lower().strip()
    if normalized.startswith("codex"):
        if re.search(r"(?mi)^\s*review comments?\s*:\s*$", review_output):
            return "fail"
        if re.search(r"(?mi)^\s*-\s*\[[A-Z0-9]+\]", review_output):
            return "fail"
        return "pass" if review_output.strip() else "unknown"
    return "unknown"


def _resolve_builtin_phase_agent(agent_mode: str, phase: str) -> tuple[str, Optional[str]]:
    """Resolve which CLI should handle a builtin executor phase."""
    normalized = normalize_agent_name(agent_mode or "dual")

    if normalized == "dual":
        return ("codex", None) if phase == "builder" else ("claude", None)
    if normalized == "codex":
        return "codex", None
    if normalized in {"claude", "claude-node"}:
        return normalized, None
    if normalized in {"claude-sonnet", "claude-opus", "claude-haiku"}:
        return "claude", normalized.split("-", 1)[1]

    raise RuntimeError(
        f"内置执行器暂时不支持任务智能体 `{agent_mode}`。"
        "请改用 codex、claude、claude-node、claude-sonnet、claude-opus、claude-haiku 或 dual。"
    )


def _run_builtin_phase(
    *,
    task: dict,
    project_path: Path,
    phase: str,
    prompt: str,
    output_path: Path,
    timeout: int,
    config_ref: str | Path | None = None,
) -> tuple[str, int, str]:
    """Execute one builtin phase with the requested agent."""
    # stub 注入钩子：e2e 测试可通过 ai._phase_stub 替换真实 CLI 调用
    from codepilot import ai as _ai_hook
    if _ai_hook._phase_stub is not None:
        return _ai_hook._phase_stub(task=task, project_path=project_path, phase=phase, prompt=prompt)

    runner, model = _resolve_builtin_phase_agent(task.get("agent", "dual"), phase)
    task_id = int(task.get("id") or 0)
    provider_ref = config_ref or project_path
    available, message = check_provider_availability(runner, project_path=provider_ref)
    if not available:
        raise RuntimeError(message)

    console_log = output_path.with_suffix(".console.log")

    if runner == "codex":
        exe = resolve_cli_provider("codex", provider_ref).find_executable()
        if not exe:
            raise RuntimeError("当前无法使用 Codex，因为本机没有找到 `codex` 命令。")

        cmd = [str(exe), "-C", str(project_path), "exec"]
        if phase == "reviewer":
            cmd.append("review")
            cmd.append("--uncommitted")
            cmd.extend(
                [
                    "--ephemeral",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-o",
                    str(output_path),
                ]
            )
        else:
            cmd.extend(
                [
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-o",
                    str(output_path),
                    prompt,
                ]
            )
        if task_id:
            exit_code, console = _run_command_live(
                cmd,
                task_id=task_id,
                phase=phase,
                log_path=console_log,
                cwd=project_path,
                timeout=timeout,
            )
        else:
            exit_code, console = _run_command(cmd, cwd=project_path, timeout=timeout)
        output = _read_output_file(output_path) or console
        label = "codex-review" if phase == "reviewer" else "codex"
        return label, exit_code, output

    provider = resolve_cli_provider(runner, provider_ref)
    exe = provider.find_executable()
    if not exe:
        raise RuntimeError(message)

    cmd = [str(exe)]
    if runner == "claude-node":
        cli_js = Path(_get_node_modules_path()) / "@anthropic-ai" / "claude-code" / "cli.js"
        if not cli_js.exists():
            raise RuntimeError(
                "当前无法使用 Claude Code (Node)，因为没有找到全局安装的 "
                "`@anthropic-ai/claude-code`。请先执行: npm install -g @anthropic-ai/claude-code"
            )
        cmd.append(str(cli_js))

    cmd.extend(["-p", "--output-format", "text", "--dangerously-skip-permissions"])
    if model:
        cmd.extend(["--model", model])

    if task_id:
        exit_code, console = _run_command_live(
            cmd,
            task_id=task_id,
            phase=phase,
            log_path=console_log,
            cwd=project_path,
            timeout=timeout,
            input_text=prompt,
        )
    else:
        exit_code, console = _run_command(cmd, cwd=project_path, timeout=timeout, input_text=prompt)
    label = runner if phase == "builder" else f"{runner}-review"
    return label, exit_code, console


def _run_builtin_executor(task: dict, project: dict, task_file: Path, auto_commit: bool = True) -> ExecutionResult:
    """Execute a task directly with Codex CLI and review the result."""
    project_path = Path(project["path"])
    config_ref = project.get("config_file") or project_path
    preflight_error = _builtin_preflight_error(project_path, auto_commit, task.get("agent", "codex"))
    if preflight_error:
        raise RuntimeError(preflight_error)

    output_dir = _builtin_runtime_dir(project)

    builder_fd, builder_raw = tempfile.mkstemp(prefix=f"task-{task['id']}-builder-", suffix=".txt", dir=output_dir)
    review_fd, review_raw = tempfile.mkstemp(prefix=f"task-{task['id']}-review-", suffix=".txt", dir=output_dir)
    os.close(builder_fd)
    os.close(review_fd)
    Path(builder_raw).unlink(missing_ok=True)
    Path(review_raw).unlink(missing_ok=True)

    builder_out = Path(builder_raw)
    review_out = Path(review_raw)

    builder_started = datetime.now()
    echo(f"[dim]  阶段: builder[/dim]")
    builder_agent, builder_exit, builder_output = _run_builtin_phase(
        task=task,
        project_path=project_path,
        phase="builder",
        prompt=_build_builtin_prompt(task, task_file),
        output_path=builder_out,
        timeout=3600,
        config_ref=config_ref,
    )
    _write_task_log(task["id"], builder_agent, "builder", builder_output, builder_exit, builder_started)
    if builder_exit != 0:
        return ExecutionResult(exit_code=builder_exit, output=builder_output, executor="builtin")

    review_started = datetime.now()
    echo(f"[dim]  阶段: reviewer[/dim]")
    review_agent, review_exit, review_output = _run_builtin_phase(
        task=task,
        project_path=project_path,
        phase="reviewer",
        prompt=_build_review_prompt(task),
        output_path=review_out,
        timeout=1800,
        config_ref=config_ref,
    )
    _write_task_log(task["id"], review_agent, "reviewer", review_output, review_exit, review_started)
    if review_exit != 0:
        return ExecutionResult(
            exit_code=review_exit,
            output=builder_output,
            review_output=review_output,
            summary="review 命令执行失败",
            executor="builtin",
        )

    verdict = _extract_review_verdict(review_output, review_agent)
    if verdict == "fail":
        return ExecutionResult(
            exit_code=2,
            output=builder_output,
            review_output=review_output,
            summary="review 未通过",
            executor="builtin",
        )
    if verdict != "pass":
        return ExecutionResult(
            exit_code=2,
            output=builder_output,
            review_output=review_output,
            summary="review 结果不明确",
            executor="builtin",
        )

    commit_sha = _git_auto_commit(project_path, task["id"], task["title"]) if auto_commit else ""
    summary_lines = [f"内置执行器完成(builder={builder_agent}, reviewer={review_agent})"]
    if commit_sha:
        summary_lines.append(f"commit: {commit_sha}")
    if verdict == "pass":
        summary_lines.append("review: pass")

    return ExecutionResult(
        exit_code=0,
        output=builder_output,
        review_output=review_output,
        summary=" | ".join(summary_lines),
        executor="builtin",
    )



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

    project_path = Path(proj["path"])
    config = _project_config(proj)
    base_branch = _resolve_project_base_branch(proj, config)
    preferred_shell = shell if shell != "auto" else ((config.shell.preferred if config else "") or "")
    shell_info = detect_best_shell(preferred_shell if preferred_shell != "auto" else None)

    dispatch_path = _find_dispatch_script(str(project_path))
    resolved_executor = executor
    if resolved_executor == "auto":
        resolved_executor = "dispatch" if dispatch_path else "builtin"

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
            preflight_error = _builtin_preflight_error(project_path, auto_commit, task.get("agent", "codex"))
        task_branch = _git_current_branch(project_path)
        # 只在 builtin 执行器启用自动分支；dispatch 模式会在工作区写任务文件，行为不受影响
        per_task_branch_enabled = (
            resolved_executor == "builtin"
            and bool(getattr(getattr(config, "automation", None), "per_task_branch", True))
        )
        if per_task_branch_enabled and not preflight_error and not dry_run:
            try:
                prepared_branch = _git_prepare_task_branch(
                    project_path,
                    task_id=task_id,
                    title=task["title"],
                    base_branch=base_branch,
                )
                if prepared_branch:
                    task_branch = prepared_branch
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

        task_file = _pick_task_file(project_path, task_id, tracked=(resolved_executor == "dispatch"))
        task_file.write_text(_build_task_md(task), encoding="utf-8")

        db.update_task(
            task_id,
            status="in_progress",
            started_at=datetime.now().isoformat(),
            branch_name=task_branch,
            worktree_path=str(project_path),
            stop_requested=0,
            stop_reason=None,
            run_phase="pending",
            heartbeat_at=datetime.now().isoformat(),
            active_pid=None,
            current_log_path=None,
            last_output="",
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
                result = _run_builtin_executor(task, proj, task_file, auto_commit=auto_commit)
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
            echo(f"[yellow]任务 #{task_id} 已停止[/yellow]")
            notify_task_status(str(project_path), task_id, task["title"], "cancelled", str(exc))
            stats["cancelled"] += 1
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

        if result.exit_code == 0 and per_task_branch_enabled:
            try:
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
            echo(f"[green][OK] 任务 #{task_id} 完成[/green]")
            notify_task_status(str(project_path), task_id, task["title"], "done")
            stats["done"] += 1
        else:
            error_message = result.summary or result.review_output or result.output or f"执行失败 (exit={result.exit_code})"
            if retry_on_failure:
                updated, should_stop = _handle_failure(task, error_message, stop_on_failure=(resolved_executor == "builtin"))
            else:
                updated = _mark_task_failed(task, error_message)
                should_stop = True
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
