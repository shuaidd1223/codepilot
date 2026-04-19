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
from codepilot.prompts import load_prompt as _load_prompt
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


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _extract_task_sections(content: str) -> dict[str, str]:
    """Parse ``task.content`` markdown into a {section_title: body} dict.

    The planner writes the task content as a sequence of ``## 验收标准`` /
    ``## Builder 职责`` / ``## 涉及文件`` / ``## 备注`` sections. We pick
    them out individually so the executor prompts can inject the right
    pieces without dumping the entire file at the AI every call.
    """
    if not content:
        return {}
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(content))
    for idx, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        sections[title] = body
    return sections


def _bullet_lines(body: str) -> list[str]:
    """Return non-empty bullet items from a markdown section body."""
    if not body:
        return []
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line in {"- 待补充", "- 无", "- （待确认）"}:
            continue
        if line.startswith(("- ", "* ")):
            items.append(line[2:].strip())
        elif line[:2].isdigit() and line[2:3] in {".", "、", ")"}:
            items.append(line[3:].strip())
        else:
            items.append(line)
    return [it for it in items if it]


def _collect_project_conventions_snippet(project_path: Path, *, max_chars: int = 800) -> str:
    """Grab the first hit of AGENTS.md / CLAUDE.md / CONTRIBUTING.md for the builder.

    Kept separate from the planner's convention block so the executor can use
    a tighter budget — the builder already has the task markdown; we only
    want the hard "don't do X" / "style must be Y" rules here.
    """
    try:
        from codepilot.ai_planner_context import _read_project_conventions
    except Exception:
        return ""
    try:
        return _read_project_conventions(
            project_path,
            max_chars_per_file=max_chars,
            total_cap=max_chars,
        )
    except Exception:
        return ""


def _build_builtin_prompt(
    task: dict,
    task_file: Path,
    *,
    project_path: Path | None = None,
    review_round: int = 1,
    previous_review_feedback: str = "",
) -> str:
    """Compose the Builder prompt.

    When ``review_round > 1`` the prompt reminds the AI this is a rework and
    injects the reviewer's prior feedback so it can fix its own mistakes
    instead of redoing everything from scratch.
    """
    sections = _extract_task_sections(task.get("content") or "")
    goal = (sections.get("任务目标") or "").strip()
    acceptance = _bullet_lines(sections.get("验收标准") or "")
    builder_notes = _bullet_lines(sections.get("Builder 职责") or "")
    files = _bullet_lines(sections.get("涉及文件") or "")

    lines: list[str] = []
    if review_round <= 1:
        lines.append(f"你正在执行排队任务 #{task['id']}：{task['title']}")
    else:
        lines.append(
            f"这是任务 #{task['id']} 「{task['title']}」的第 {review_round} 轮重做。"
            " 上一轮 reviewer 发现了阻塞问题，请针对性修复。"
        )
    lines.append("")

    if goal:
        lines.append("【任务目标】")
        lines.append(goal)
        lines.append("")

    if acceptance:
        lines.append("【验收标准（必须全部达成，reviewer 会逐条核对）】")
        for i, item in enumerate(acceptance, 1):
            lines.append(f"  {i}. {item}")
        lines.append("")

    if builder_notes:
        lines.append("【实施提示（来自规划器）】")
        for item in builder_notes:
            lines.append(f"  - {item}")
        lines.append("")

    if files:
        lines.append("【预计要动的文件（非强制，偏离请在 Summary 说明）】")
        for item in files:
            lines.append(f"  - {item}")
        lines.append("")

    conventions = ""
    if project_path is not None:
        conventions = _collect_project_conventions_snippet(project_path)
    if conventions:
        lines.append("【项目约定（来自 AGENTS.md / CLAUDE.md / CONTRIBUTING.md 等，必须遵守）】")
        lines.append(conventions)
        lines.append("")

    if review_round > 1 and previous_review_feedback:
        lines.append("【上一轮 reviewer 的阻塞意见（必须处理）】")
        lines.append(previous_review_feedback.strip())
        lines.append("")

    lines.append(_load_prompt("builder_rules", task_file=str(task_file)).rstrip())
    return "\n".join(lines)


def _build_review_prompt(
    task: dict,
    *,
    review_round: int = 1,
) -> str:
    """Compose the Reviewer prompt with acceptance-criteria-driven checklist."""
    sections = _extract_task_sections(task.get("content") or "")
    acceptance = _bullet_lines(sections.get("验收标准") or "")
    reviewer_notes = _bullet_lines(sections.get("Reviewer 职责") or "")

    lines = [
        f"请审查当前仓库中为任务 #{task['id']} `{task['title']}` 产生的未提交改动。",
    ]
    if review_round > 1:
        lines.append(f"（这是第 {review_round} 轮审查，之前有过 FAIL 结论，请重新判断）")

    if acceptance:
        lines.append("")
        lines.append("【必须逐条核对的验收标准】")
        for i, item in enumerate(acceptance, 1):
            lines.append(f"  {i}. {item}")
        lines.append(
            "对每一条，明确指出: 通过 / 未通过 / 无法判断，并说明理由（看了哪些文件或命令输出）。"
        )

    if reviewer_notes:
        lines.append("")
        lines.append("【补充检查项（来自规划器的 reviewer 提示）】")
        for item in reviewer_notes:
            lines.append(f"  - {item}")

    lines.append("")
    lines.append(_load_prompt("reviewer_rules").rstrip())
    return "\n".join(lines)


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


def _extract_reviewer_findings(review_output: str) -> str:
    """Pull the actionable failure summary out of a reviewer transcript.

    We want only the "what's wrong" part handed to the builder as its next
    round of input — not the entire reviewer chain-of-thought (which could
    include prose, verdict line, etc.). Strategy:

    1. Prefer content between "需要修复的点" and "VERDICT:" if the reviewer
       followed the prompt.
    2. Otherwise drop the final VERDICT line and pass the rest.
    """
    if not review_output:
        return ""
    text = review_output.strip()

    # Strategy 1: section between a "needs fix" header and the VERDICT line.
    pattern = re.compile(
        r"(?:需要修复的点|需要修复|需要处理|修复建议)\s*[:：]?\s*\n(.*?)(?:\n\s*VERDICT\s*:|$)",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(text)
    if m:
        block = m.group(1).strip()
        if block:
            return block

    # Strategy 2: drop the VERDICT line and return the rest, capped.
    lines = [ln for ln in text.splitlines() if not re.match(r"\s*VERDICT\s*:", ln, re.I)]
    stripped = "\n".join(lines).strip()
    if len(stripped) > 2000:
        stripped = stripped[-2000:]
    return stripped


@dataclass
class _PhaseOutcome:
    """Normalized result of one builder or reviewer invocation."""

    agent: str
    exit_code: int
    output: str


@dataclass
class _ExecutorContext:
    """Everything a single executor run needs once, so helpers don't each
    recompute project paths / config refs / event hooks."""

    task: dict
    project: dict
    project_path: Path
    config_ref: object
    output_dir: Path
    task_file: Path
    max_rounds: int
    task_id_for_events: Optional[int]


def _make_phase_output_path(output_dir: Path, task_id: int, round_num: int, kind: str) -> Path:
    """Reserve a unique file path under ``output_dir`` for a phase's output.

    We use ``mkstemp`` for uniqueness, then delete it immediately because some
    CLI providers (codex/claude) refuse to overwrite an existing ``-o`` file.
    """
    fd, raw = tempfile.mkstemp(
        prefix=f"task-{task_id}-{kind}-r{round_num}-",
        suffix=".txt",
        dir=output_dir,
    )
    os.close(fd)
    path = Path(raw)
    path.unlink(missing_ok=True)
    return path


def _run_builder_round(
    ctx: _ExecutorContext,
    *,
    round_num: int,
    previous_findings: str,
) -> _PhaseOutcome:
    """Run one builder invocation; persist a task_log row on completion."""
    from codepilot import progress_bus

    output_path = _make_phase_output_path(ctx.output_dir, ctx.task["id"], round_num, "builder")
    label = "builder" if round_num == 1 else f"builder (round {round_num}/{ctx.max_rounds})"
    echo(f"[dim]  阶段: {label}[/dim]")
    progress_bus.emit(
        task_id=ctx.task_id_for_events,
        stage="builder",
        message=f"启动 {label}",
        extra={"round": round_num, "round_total": ctx.max_rounds},
    )

    started = datetime.now()
    prompt = _build_builtin_prompt(
        ctx.task,
        ctx.task_file,
        project_path=ctx.project_path,
        review_round=round_num,
        previous_review_feedback=previous_findings,
    )
    agent, exit_code, output = _run_builtin_phase(
        task=ctx.task,
        project_path=ctx.project_path,
        phase="builder",
        prompt=prompt,
        output_path=output_path,
        timeout=3600,
        config_ref=ctx.config_ref,
    )
    phase_name = "builder" if round_num == 1 else f"builder-r{round_num}"
    _write_task_log(ctx.task["id"], agent, phase_name, output, exit_code, started)
    return _PhaseOutcome(agent=agent, exit_code=exit_code, output=output)


def _run_reviewer_round(
    ctx: _ExecutorContext,
    *,
    round_num: int,
) -> _PhaseOutcome:
    """Run one reviewer invocation; persist a task_log row on completion."""
    from codepilot import progress_bus

    output_path = _make_phase_output_path(ctx.output_dir, ctx.task["id"], round_num, "review")
    label = "reviewer" if round_num == 1 else f"reviewer (round {round_num}/{ctx.max_rounds})"
    echo(f"[dim]  阶段: {label}[/dim]")
    progress_bus.emit(
        task_id=ctx.task_id_for_events,
        stage="reviewer",
        message=f"启动 {label}",
        extra={"round": round_num, "round_total": ctx.max_rounds},
    )

    started = datetime.now()
    prompt = _build_review_prompt(ctx.task, review_round=round_num)
    agent, exit_code, output = _run_builtin_phase(
        task=ctx.task,
        project_path=ctx.project_path,
        phase="reviewer",
        prompt=prompt,
        output_path=output_path,
        timeout=1800,
        config_ref=ctx.config_ref,
    )
    phase_name = "reviewer" if round_num == 1 else f"reviewer-r{round_num}"
    _write_task_log(ctx.task["id"], agent, phase_name, output, exit_code, started)
    return _PhaseOutcome(agent=agent, exit_code=exit_code, output=output)


def _finalize_executor_success(
    ctx: _ExecutorContext,
    *,
    auto_commit: bool,
    round_num: int,
    builder: _PhaseOutcome,
    reviewer: _PhaseOutcome,
) -> ExecutionResult:
    """After a PASS verdict, optionally commit and build the success summary."""
    commit_sha = (
        _git_auto_commit(ctx.project_path, ctx.task["id"], ctx.task["title"])
        if auto_commit
        else ""
    )
    parts = [
        f"内置执行器完成(builder={builder.agent}, reviewer={reviewer.agent}, rounds={round_num})"
    ]
    if commit_sha:
        parts.append(f"commit: {commit_sha}")
    parts.append("review: pass")
    return ExecutionResult(
        exit_code=0,
        output=builder.output,
        review_output=reviewer.output,
        summary=" | ".join(parts),
        executor="builtin",
    )


def _run_builtin_executor(
    task: dict,
    project: dict,
    task_file: Path,
    auto_commit: bool = True,
    *,
    max_review_rounds: int = 2,
) -> ExecutionResult:
    """Execute a task with Codex/Claude CLI, review, and retry on FAIL.

    The builder and reviewer form a short loop: a FAIL verdict (or an unclear
    one) feeds the reviewer's findings back into the builder for another pass,
    up to ``max_review_rounds`` attempts. All the single-round mechanics live
    in ``_run_builder_round`` / ``_run_reviewer_round``; this function only
    owns the loop, the verdict decision, and the final commit/summary step.
    """
    project_path = Path(project["path"])
    preflight_error = _builtin_preflight_error(project_path, auto_commit, task.get("agent", "codex"))
    if preflight_error:
        raise RuntimeError(preflight_error)

    from codepilot import progress_bus

    ctx = _ExecutorContext(
        task=task,
        project=project,
        project_path=project_path,
        config_ref=project.get("config_file") or project_path,
        output_dir=_builtin_runtime_dir(project),
        task_file=task_file,
        max_rounds=max(1, int(max_review_rounds or 1)),
        task_id_for_events=(int(task.get("id") or 0) or None),
    )

    previous_findings = ""
    builder = _PhaseOutcome(agent="", exit_code=0, output="")
    reviewer = _PhaseOutcome(agent="", exit_code=0, output="")

    for round_num in range(1, ctx.max_rounds + 1):
        builder = _run_builder_round(ctx, round_num=round_num, previous_findings=previous_findings)
        if builder.exit_code != 0:
            return ExecutionResult(
                exit_code=builder.exit_code,
                output=builder.output,
                executor="builtin",
            )

        reviewer = _run_reviewer_round(ctx, round_num=round_num)
        if reviewer.exit_code != 0:
            return ExecutionResult(
                exit_code=reviewer.exit_code,
                output=builder.output,
                review_output=reviewer.output,
                summary="review 命令执行失败",
                executor="builtin",
            )

        verdict = _extract_review_verdict(reviewer.output, reviewer.agent)
        if verdict == "pass":
            progress_bus.emit(
                task_id=ctx.task_id_for_events,
                stage="reviewer",
                message="reviewer 判定 PASS",
                extra={"round": round_num, "verdict": "pass"},
            )
            return _finalize_executor_success(
                ctx,
                auto_commit=auto_commit,
                round_num=round_num,
                builder=builder,
                reviewer=reviewer,
            )

        previous_findings = _extract_reviewer_findings(reviewer.output)
        if round_num >= ctx.max_rounds:
            summary = (
                "review 未通过（已用完重做轮次）"
                if verdict == "fail"
                else "review 结果不明确（已用完重做轮次）"
            )
            progress_bus.emit(
                task_id=ctx.task_id_for_events,
                stage="reviewer",
                level="error",
                message=summary,
                extra={"round": round_num, "verdict": verdict},
            )
            return ExecutionResult(
                exit_code=2,
                output=builder.output,
                review_output=reviewer.output,
                summary=summary,
                executor="builtin",
            )

        progress_bus.emit(
            task_id=ctx.task_id_for_events,
            stage="reviewer",
            level="warning",
            message=f"reviewer 判定 {verdict.upper()}，准备第 {round_num + 1} 轮重做",
            extra={"round": round_num, "verdict": verdict},
        )
        echo(
            f"[yellow]  reviewer 判定 {verdict.upper()}，准备第 {round_num + 1} 轮重做，"
            f"让 builder 针对反馈再改一次[/yellow]"
        )

    # Unreachable: the loop either returns success, returns a failure summary
    # when rounds are exhausted, or bails out on a non-zero phase exit code.
    raise RuntimeError("_run_builtin_executor: unreachable fallthrough")



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
                result = _run_builtin_executor(
                    task,
                    proj,
                    task_file,
                    auto_commit=auto_commit,
                    max_review_rounds=max_review_rounds,
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
