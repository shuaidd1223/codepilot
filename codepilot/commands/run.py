"""Run queued tasks via external dispatch or a built-in Codex executor."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import click

from codepilot import db
from codepilot.ai import (
    _get_node_modules_path,
    check_provider_availability,
    normalize_agent_name,
    resolve_cli_provider,
)
from codepilot.commands.status import _resolve_project
from codepilot.config import load_config
from codepilot.output import echo
from codepilot.webhook import notify_task_status


@dataclass
class ShellInfo:
    """Resolved shell configuration for dispatch scripts."""

    executable: str
    args: list[str]
    is_powershell: bool = False
    is_bash: bool = False
    version_hint: str = ""


@dataclass
class ExecutionResult:
    """Normalized task execution result."""

    exit_code: int
    output: str = ""
    review_output: str = ""
    summary: str = ""
    executor: str = "dispatch"


def detect_best_shell(preferred: Optional[str] = None) -> ShellInfo:
    """Pick the best available shell on the current platform."""
    import platform
    import shutil

    system = platform.system().lower()
    is_windows = system == "windows"
    requested = (preferred or "").lower().strip()

    if requested in {"pwsh", "powershell7"} and shutil.which("pwsh"):
        return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
    if requested == "powershell":
        if shutil.which("powershell.exe"):
            return ShellInfo("powershell.exe", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 5")
        if shutil.which("pwsh"):
            return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
    if requested in {"bash", "zsh", "sh"}:
        shell = shutil.which(requested)
        if shell:
            return ShellInfo(shell, ["-c"], is_bash=True, version_hint=requested)

    if is_windows:
        if shutil.which("pwsh"):
            return ShellInfo("pwsh", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 7")
        if shutil.which("powershell.exe"):
            return ShellInfo("powershell.exe", ["-ExecutionPolicy", "Bypass"], is_powershell=True, version_hint="PowerShell 5")
        return ShellInfo("cmd.exe", ["/C"], version_hint="cmd")

    for shell_name in ["zsh", "bash", "sh"]:
        shell = shutil.which(shell_name)
        if shell:
            return ShellInfo(shell, ["-c"], is_bash=True, version_hint=shell_name)
    return ShellInfo("sh", ["-c"], is_bash=True, version_hint="sh")


def build_script_command(shell: ShellInfo, script_path: Path, script_args: list[str]) -> tuple[list[str], str]:
    """Build a portable script invocation command."""
    if shell.is_powershell:
        cmd = [shell.executable] + shell.args + ["-File", str(script_path)] + script_args
        return cmd, f"{shell.version_hint} -File {script_path.name}"
    if shell.is_bash:
        quoted_args = " ".join(f'"{arg}"' for arg in script_args)
        script = f'chmod +x "{script_path}" 2>/dev/null; "{script_path}" {quoted_args}'
        cmd = [shell.executable] + shell.args + [script]
        return cmd, f"{shell.version_hint} {script_path.name}"
    cmd = [shell.executable] + shell.args + [f'"{script_path}" {" ".join(script_args)}']
    return cmd, f"cmd {script_path.name}"


def _project_config(project_path: str | None):
    config_path = Path(project_path).resolve() / "AGENTS.toml" if project_path else None
    return load_config(config_path if config_path and config_path.exists() else None)


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


def _run_command(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = 3600,
    input_text: Optional[str] = None,
) -> tuple[int, str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        input=input_text,
    )
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    return result.returncode, output


def _git_current_branch(project_path: Path) -> str:
    code, output = _run_command(["git", "branch", "--show-current"], cwd=project_path, timeout=30)
    if code == 0 and output.strip():
        return output.strip().splitlines()[-1]
    return "main"


def _git_is_repo(project_path: Path) -> bool:
    code, output = _run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=project_path, timeout=30)
    return code == 0 and output.strip().lower().endswith("true")


def _git_has_changes(project_path: Path) -> bool:
    code, output = _run_command(["git", "status", "--short"], cwd=project_path, timeout=30)
    return code == 0 and bool(output.strip())


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


def _builtin_preflight_error(project_path: Path, auto_commit: bool) -> str:
    """Return a human-readable reason why builtin execution should not start yet."""
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


def _git_auto_commit(project_path: Path, task_id: int, title: str) -> str:
    if not _git_has_changes(project_path):
        return ""

    add_code, add_output = _run_command(["git", "add", "-A"], cwd=project_path, timeout=120)
    if add_code != 0:
        raise RuntimeError(f"git add 失败:\n{add_output}")

    diff_code, _ = _run_command(["git", "diff", "--cached", "--quiet"], cwd=project_path, timeout=30)
    if diff_code == 0:
        return ""

    safe_title = " ".join(title.strip().split())[:60]
    commit_msg = f"task #{task_id}: {safe_title}"
    commit_code, commit_output = _run_command(["git", "commit", "-m", commit_msg], cwd=project_path, timeout=300)
    if commit_code != 0:
        raise RuntimeError(f"git commit 失败:\n{commit_output}")

    sha_code, sha_output = _run_command(["git", "rev-parse", "--short", "HEAD"], cwd=project_path, timeout=30)
    return sha_output.strip() if sha_code == 0 else ""


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


def _extract_review_verdict(review_output: str) -> str:
    for line in reversed(review_output.splitlines()):
        marker = line.strip().upper()
        if marker == "VERDICT: PASS":
            return "pass"
        if marker == "VERDICT: FAIL":
            return "fail"
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
) -> tuple[str, int, str]:
    """Execute one builtin phase with the requested agent."""
    runner, model = _resolve_builtin_phase_agent(task.get("agent", "dual"), phase)
    available, message = check_provider_availability(runner, project_path=project_path)
    if not available:
        raise RuntimeError(message)

    if runner == "codex":
        exe = resolve_cli_provider("codex", project_path).find_executable()
        if not exe:
            raise RuntimeError("当前无法使用 Codex，因为本机没有找到 `codex` 命令。")

        cmd = [str(exe), "-C", str(project_path), "exec"]
        if phase == "reviewer":
            cmd.append("review")
            cmd.append("--uncommitted")
            cmd.extend(
                [
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-o",
                    str(output_path),
                ]
            )
        else:
            cmd.extend(
                [
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-o",
                    str(output_path),
                    prompt,
                ]
            )
        exit_code, console = _run_command(cmd, cwd=project_path, timeout=timeout)
        output = _read_output_file(output_path) or console
        label = "codex-review" if phase == "reviewer" else "codex"
        return label, exit_code, output

    provider = resolve_cli_provider(runner, project_path)
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

    exit_code, console = _run_command(cmd, cwd=project_path, timeout=timeout, input_text=prompt)
    label = runner if phase == "builder" else f"{runner}-review"
    return label, exit_code, console


def _run_builtin_executor(task: dict, project: dict, task_file: Path, auto_commit: bool = True) -> ExecutionResult:
    """Execute a task directly with Codex CLI and review the result."""
    project_path = Path(project["path"])
    preflight_error = _builtin_preflight_error(project_path, auto_commit)
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
    builder_agent, builder_exit, builder_output = _run_builtin_phase(
        task=task,
        project_path=project_path,
        phase="builder",
        prompt=_build_builtin_prompt(task, task_file),
        output_path=builder_out,
        timeout=3600,
    )
    _write_task_log(task["id"], builder_agent, "builder", builder_output, builder_exit, builder_started)
    if builder_exit != 0:
        return ExecutionResult(exit_code=builder_exit, output=builder_output, executor="builtin")

    review_started = datetime.now()
    review_agent, review_exit, review_output = _run_builtin_phase(
        task=task,
        project_path=project_path,
        phase="reviewer",
        prompt=_build_review_prompt(task),
        output_path=review_out,
        timeout=1800,
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

    verdict = _extract_review_verdict(review_output)
    if verdict == "fail":
        return ExecutionResult(
            exit_code=2,
            output=builder_output,
            review_output=review_output,
            summary="review 未通过",
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

    if _is_powershell_script(dispatch_path):
        cmd, _ = build_script_command(shell, dispatch_path, script_args)
    else:
        cmd, _ = build_script_command(shell, dispatch_path, script_args)
    return _run_command(cmd, timeout=3600)


def _handle_failure(task: dict, error_message: str, stop_on_failure: bool = False) -> tuple[dict, bool]:
    updated = db.increment_task_retry(task["id"], error_message[:4000])
    should_stop = stop_on_failure or updated["status"] == "failed"
    return updated, should_stop


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
) -> dict:
    """Execute up to `limit` runnable tasks for a project."""
    db.init_db()
    proj = db.get_project(project)
    if not proj:
        raise RuntimeError(f"项目 '{project}' 未注册，请先运行 codepilot init")

    project_path = Path(proj["path"])
    config = _project_config(str(project_path))
    preferred_shell = shell if shell != "auto" else (config.shell.preferred or "")
    shell_info = detect_best_shell(preferred_shell if preferred_shell != "auto" else None)

    dispatch_path = _find_dispatch_script(str(project_path))
    resolved_executor = executor
    if resolved_executor == "auto":
        resolved_executor = "dispatch" if dispatch_path else "builtin"

    echo(f"[dim]使用执行器: {resolved_executor}[/dim]")
    if resolved_executor == "dispatch":
        echo(f"[dim]使用 Shell: {shell_info.version_hint}[/dim]")

    stats = {"processed": 0, "done": 0, "failed": 0, "requeued": 0, "executor": resolved_executor}

    for _ in range(limit):
        tasks = db.next_backlog_task(project)
        if not tasks:
            echo("[yellow]没有待执行的任务[/yellow]")
            break

        task = tasks[0]
        task_id = task["id"]
        preflight_error = ""
        if resolved_executor == "builtin":
            preflight_error = _builtin_preflight_error(project_path, auto_commit)
        if preflight_error:
            db.update_task(task_id, status="backlog", error_message=preflight_error)
            echo(f"[yellow]{preflight_error}[/yellow]")
            stats["processed"] += 1
            stats["requeued"] += 1
            break

        task_file = _pick_task_file(project_path, task_id, tracked=(resolved_executor == "dispatch"))
        task_file.write_text(_build_task_md(task), encoding="utf-8")

        db.update_task(
            task_id,
            status="in_progress",
            started_at=datetime.now().isoformat(),
            branch_name=_git_current_branch(project_path),
            worktree_path=str(project_path),
        )

        echo(f"[cyan]-> 执行任务 #{task_id}[/cyan]  {task['title']}")
        click.echo(f"  Agent: {task['agent']}  优先级: {task['priority']}")
        click.echo(f"  任务文件: {task_file}")

        if dry_run:
            db.update_task(task_id, status="backlog", started_at=None)
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
        except Exception as exc:
            updated, should_stop = _handle_failure(task, str(exc), stop_on_failure=(resolved_executor == "builtin"))
            echo(f"[red]执行出错: {exc}[/red]")
            if updated["status"] == "failed":
                stats["failed"] += 1
                notify_task_status(str(project_path), task_id, task["title"], "failed", str(exc))
            else:
                stats["requeued"] += 1
            stats["processed"] += 1
            if should_stop or once:
                break
            continue

        if result.exit_code == 0:
            db.update_task(
                task_id,
                status="done",
                completed_at=datetime.now().isoformat(),
                error_message="",
                delivery_record=result.summary or result.review_output or result.output,
            )
            echo(f"[green][OK] 任务 #{task_id} 完成[/green]")
            notify_task_status(str(project_path), task_id, task["title"], "done")
            stats["done"] += 1
        else:
            error_message = result.summary or result.review_output or result.output or f"执行失败 (exit={result.exit_code})"
            updated, should_stop = _handle_failure(task, error_message, stop_on_failure=(resolved_executor == "builtin"))
            if updated["status"] == "failed":
                echo(f"[red][X] 任务 #{task_id} 失败[/red]")
                stats["failed"] += 1
                notify_task_status(str(project_path), task_id, task["title"], "failed", error_message)
            else:
                echo(f"[yellow][!] 任务 #{task_id} 已回到 backlog，等待重试[/yellow]")
                stats["requeued"] += 1
            if should_stop:
                stats["processed"] += 1
                break

        if result.output:
            lines = [line for line in result.output.splitlines() if line.strip()]
            if lines:
                echo("[dim]--- builder 输出 ---[/dim]")
                for line in lines[-8:]:
                    click.echo(f"  {line}")
        if result.review_output:
            lines = [line for line in result.review_output.splitlines() if line.strip()]
            if lines:
                echo("[dim]--- reviewer 输出 ---[/dim]")
                for line in lines[-8:]:
                    click.echo(f"  {line}")
        click.echo()

        stats["processed"] += 1
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
    """Execute queued tasks for a registered project."""
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
        echo(f"[red]{exc}[/red]")
        return

    echo(
        f"\n[dim]Run 完成: processed={stats['processed']} done={stats['done']} "
        f"failed={stats['failed']} requeued={stats['requeued']}[/dim]"
    )
