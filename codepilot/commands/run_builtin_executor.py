"""Phase execution and orchestration for the built-in executor."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from codepilot.ai_support.executor_contract import (
    ExecutorFallbackReason,
    append_executor_telemetry_marker,
    build_executor_fallback_telemetry,
    classify_executor_fallback_reason,
)
from codepilot.ai_support.family_runtime import build_env_for_family
from codepilot.ai_support.service import _get_node_modules_path, normalize_agent_name
from codepilot.core.config import AgentsConfig, load_project_config
from codepilot.commands.execution_artifacts import (
    collect_git_patch_artifact,
    review_artifact_from_output,
    validation_artifact_from_result,
)
from codepilot.core.output import echo
from codepilot.core.task_mutation_guard import runner_task_context_env
from codepilot.commands.reviewer_output import ReviewerVerdict, format_findings_for_builder, parse_reviewer_output
from codepilot.commands.run_shell import PreflightSkipError
from codepilot.commands.run_builtin_core import (
    ExecutionResult,
    _resolve_dual_phase_agents_for_task,
    _runner_module,
)
from codepilot.commands.run_builtin_prompts import _build_builtin_prompt, _build_review_prompt


def _resolve_builtin_single_agent(agent_mode: str) -> tuple[str, Optional[str]]:
    """Resolve one concrete agent into a builtin CLI runner + optional model."""
    normalized = normalize_agent_name(agent_mode or "codex")

    if normalized == "codex":
        return "codex", None
    if normalized in {"claude", "claude-node"}:
        return normalized, None
    if normalized in {"claude-sonnet", "claude-opus", "claude-haiku"}:
        return "claude", normalized.split("-", 1)[1]
    if normalized == "opencode":
        return "opencode", None

    raise RuntimeError(
        f"内置执行器暂时不支持任务智能体 `{agent_mode}`。"
        "请改用 codex、claude、claude-node、claude-sonnet、claude-opus、claude-haiku、opencode 或 dual。"
    )


def _resolve_builtin_phase_agent(
    agent_mode: str,
    phase: str,
    *,
    task: Optional[dict] = None,
    project_ref: str | Path | dict | None = None,
) -> tuple[str, Optional[str]]:
    """Resolve which CLI should handle a builtin executor phase."""
    normalized = normalize_agent_name(agent_mode or "dual")

    if normalized == "dual":
        builder_agent, reviewer_agent = _resolve_dual_phase_agents_for_task(task, project_ref)
        selected = builder_agent if phase == "builder" else reviewer_agent
        return _resolve_builtin_single_agent(selected)
    return _resolve_builtin_single_agent(normalized)


def _agent_label_runner(agent_label: str) -> str:
    """Collapse persisted phase labels like ``codex-review`` to runner names."""
    label = str(agent_label or "").strip().lower()
    if label.startswith("codex"):
        return "codex"
    if label.startswith("claude-node"):
        return "claude-node"
    if label.startswith("claude"):
        return "claude"
    return label


def _is_builtin_agent_tooling_failure(agent_label: str, output: str) -> bool:
    """Return whether a phase failed because its CLI/model is unavailable."""
    return (
        classify_executor_fallback_reason(agent_label, output)
        is not ExecutorFallbackReason.NONE
    )


def _family_runtime_name_for_runner(runner: str) -> str:
    """Map concrete builtin runners to the env-bridge family name."""
    return "claude" if runner == "claude-node" else runner


def _build_builtin_phase_env_overrides(
    runner: str,
    provider_ref: str | Path | dict | None,
) -> dict[str, str]:
    """Build the minimal env overlay for the CLI family subprocess."""
    cfg = load_project_config(provider_ref) or AgentsConfig.from_dict({})
    return build_env_for_family(_family_runtime_name_for_runner(runner), cfg)


def _overlay_process_env(env_overrides: dict[str, str]) -> dict[str, Optional[str]]:
    """Apply temporary process env overrides and return the original values."""
    saved_env: dict[str, Optional[str]] = {}
    for key, value in env_overrides.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value
    return saved_env


def _restore_process_env(saved_env: dict[str, Optional[str]]) -> None:
    """Undo a temporary process env overlay."""
    for key, original in saved_env.items():
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


def _expected_phase_agent_label(
    task: dict,
    phase: str,
    config_ref: str | Path | dict | None,
) -> str:
    """Return the label we expect ``_run_builtin_phase`` to use."""
    runner, _model = _resolve_builtin_phase_agent(
        task.get("agent", "dual"),
        phase,
        task=task,
        project_ref=config_ref,
    )
    if phase == "reviewer":
        return "codex-review" if runner == "codex" else f"{runner}-review"
    return "codex" if runner == "codex" else runner


def _phase_model_for_agent_label(ctx: "_ExecutorContext", phase: str, agent_label: str) -> str:
    """Return the configured model for the initial phase agent when known."""
    try:
        runner, model = _resolve_builtin_phase_agent(
            ctx.task.get("agent", "dual"),
            phase,
            task=ctx.task,
            project_ref=ctx.config_ref,
        )
    except Exception:
        return ""
    if _agent_label_runner(agent_label) == runner:
        return model or ""
    return ""


def _model_for_agent_override(agent_mode: str) -> str:
    """Return the explicit model component for a fallback override."""
    try:
        _runner, model = _resolve_builtin_single_agent(agent_mode)
    except Exception:
        return ""
    return model or ""


@dataclass
class _ExecutorContext:
    """Everything a single executor run needs once."""

    task: dict
    project: dict
    project_path: Path
    config_ref: object
    output_dir: Path
    task_file: Path
    max_rounds: int
    task_id_for_events: Optional[int]
    silence_timeout: int = 0


def _agent_language_for_context(ctx: _ExecutorContext) -> str:
    cfg = load_project_config(ctx.config_ref or ctx.project_path) or AgentsConfig.from_dict({})
    return str(getattr(cfg.automation, "agent_language", "en") or "en")


def _dirty_worktree_policy_for_project(project: dict, project_path: Path) -> str:
    try:
        cfg = load_project_config(project) or load_project_config(project_path) or AgentsConfig.from_dict({})
        return str(getattr(cfg.automation, "preflight_dirty_worktree", "stop") or "stop")
    except Exception:
        return "stop"


def _select_builtin_phase_fallback_agent(
    ctx: _ExecutorContext,
    *,
    phase: str,
    failed_agent: str,
    output: str,
) -> Optional[str]:
    """Choose another dual-mode phase agent for CLI/tooling failures."""
    if not _is_builtin_agent_tooling_failure(failed_agent, output):
        return None

    try:
        normalized = normalize_agent_name(str(ctx.task.get("agent") or "dual"))
    except Exception:
        return None
    if normalized != "dual":
        return None

    try:
        builder_agent, reviewer_agent = _resolve_dual_phase_agents_for_task(ctx.task, ctx.config_ref)
    except Exception:
        return None

    preferred = reviewer_agent if phase == "builder" else builder_agent
    candidates = [preferred, "claude", "claude-node", "codex", "opencode"]
    failed_runner = _agent_label_runner(failed_agent)
    seen: set[str] = set()
    runner_mod = _runner_module()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            normalized_candidate = normalize_agent_name(str(candidate))
            runner, _model = _resolve_builtin_single_agent(normalized_candidate)
        except Exception:
            continue
        if runner == failed_runner or normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        available, _message = runner_mod.check_provider_availability(runner, project_path=ctx.config_ref)
        if available:
            return normalized_candidate
    return None


def _run_builtin_phase(
    *,
    task: dict,
    project_path: Path,
    phase: str,
    prompt: str,
    output_path: Path,
    timeout: int,
    config_ref: str | Path | None = None,
    display_phase: Optional[str] = None,
    silence_timeout_seconds: int = 0,
    agent_override: Optional[str] = None,
) -> tuple[str, int, str]:
    """Execute one builtin phase with the requested agent."""
    from codepilot.ai_support import service as _ai_hook

    if _ai_hook._phase_stub is not None:
        return _ai_hook._phase_stub(task=task, project_path=project_path, phase=phase, prompt=prompt)

    if agent_override:
        runner, model = _resolve_builtin_single_agent(agent_override)
    else:
        runner, model = _resolve_builtin_phase_agent(
            task.get("agent", "dual"),
            phase,
            task=task,
            project_ref=config_ref or project_path,
        )
    task_id = int(task.get("id") or 0)
    heartbeat_phase = display_phase or phase
    provider_ref = config_ref or project_path
    runner_mod = _runner_module()
    available, message = runner_mod.check_provider_availability(runner, project_path=provider_ref)
    if not available:
        raise RuntimeError(message)

    console_log = output_path.with_suffix(".console.md")
    env_overrides = _build_builtin_phase_env_overrides(runner, provider_ref)
    env_overrides.update(runner_task_context_env(task_id, phase))
    saved_env = _overlay_process_env(env_overrides)

    try:
        if runner == "codex":
            exe = runner_mod.resolve_cli_provider("codex", provider_ref).find_executable()
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
                exit_code, console = runner_mod._run_command_live(
                    cmd,
                    task_id=task_id,
                    phase=heartbeat_phase,
                    log_path=console_log,
                    cwd=project_path,
                    timeout=timeout,
                    silence_timeout_seconds=silence_timeout_seconds,
                )
            else:
                exit_code, console = runner_mod._run_command(cmd, cwd=project_path, timeout=timeout)
            output = runner_mod._read_output_file(output_path) or console
            label = "codex-review" if phase == "reviewer" else "codex"
            return label, exit_code, output

        provider = runner_mod.resolve_cli_provider(runner, provider_ref)
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

        if runner == "opencode":
            cmd.append("run")
        else:
            cmd.extend(["-p", "--output-format", "text", "--dangerously-skip-permissions"])
            if model:
                cmd.extend(["--model", model])

        if task_id:
            exit_code, console = runner_mod._run_command_live(
                cmd,
                task_id=task_id,
                phase=heartbeat_phase,
                log_path=console_log,
                cwd=project_path,
                timeout=timeout,
                input_text=prompt,
                silence_timeout_seconds=silence_timeout_seconds,
            )
        else:
            exit_code, console = runner_mod._run_command(cmd, cwd=project_path, timeout=timeout, input_text=prompt)
    finally:
        _restore_process_env(saved_env)
    label = runner if phase == "builder" else f"{runner}-review"
    return label, exit_code, console


def _extract_reviewer_findings(review_output: str) -> str:
    """Return the actionable failure summary for the builder's next round."""
    verdict_model = parse_reviewer_output(review_output or "")
    if verdict_model.blockers:
        formatted = format_findings_for_builder(verdict_model)
        if formatted:
            return formatted
    return ""


def _review_verdict_event_extra(verdict_model: ReviewerVerdict, *, round_num: int) -> dict:
    """Build a structured progress payload for reviewer verdict events."""
    return {
        "round": round_num,
        "review_verdict": True,
        "verdict": verdict_model.verdict,
        "source": verdict_model.source,
        "blockers": list(verdict_model.blockers),
        "blocker_count": len(verdict_model.blockers),
        "advisory": list(verdict_model.advisory),
        "advisory_count": len(verdict_model.advisory),
        "ac_checks": [dict(item) for item in verdict_model.ac_checks],
    }


def _display_phase_name(kind: str, *, round_num: int, round_total: int) -> str:
    """Return the phase label used by live progress / Web UI."""
    base = str(kind or "").strip() or "phase"
    return base if round_num <= 1 else f"{base} r{round_num}/{round_total}"


@dataclass
class _PhaseOutcome:
    """Normalized result of one builder or reviewer invocation."""

    agent: str
    exit_code: int
    output: str
    display_phase: str = ""


def _make_phase_output_path(output_dir: Path, task_id: int, round_num: int, kind: str) -> Path:
    """Reserve a unique file path under ``output_dir`` for a phase's output."""
    fd, raw = tempfile.mkstemp(
        prefix=f"task-{task_id}-{kind}-r{round_num}-",
        suffix=".md",
        dir=output_dir,
    )
    os.close(fd)
    path = Path(raw)
    path.unlink(missing_ok=True)
    return path


def _run_phase_with_tooling_fallback(
    ctx: _ExecutorContext,
    *,
    phase: str,
    round_num: int,
    phase_name: str,
    label: str,
    display_phase: str,
    prompt: str,
    output_path: Path,
    timeout: int,
) -> tuple[str, int, str, datetime]:
    """Run one phase and, for dual mode, switch agent on CLI/model failures."""
    from codepilot.core import progress_bus

    runner_mod = _runner_module()
    started = datetime.now()
    try:
        agent, exit_code, output = runner_mod._run_builtin_phase(
            task=ctx.task,
            project_path=ctx.project_path,
            phase=phase,
            prompt=prompt,
            output_path=output_path,
            timeout=timeout,
            config_ref=ctx.config_ref,
            display_phase=display_phase,
            silence_timeout_seconds=ctx.silence_timeout,
        )
    except Exception as exc:
        agent = _expected_phase_agent_label(ctx.task, phase, ctx.config_ref)
        exit_code = 1
        output = str(exc)
        fallback_agent = _select_builtin_phase_fallback_agent(
            ctx,
            phase=phase,
            failed_agent=agent,
            output=output,
        )
        if not fallback_agent:
            raise
    else:
        fallback_agent = _select_builtin_phase_fallback_agent(
            ctx,
            phase=phase,
            failed_agent=agent,
            output=output,
        )

    if not fallback_agent:
        return agent, exit_code, output, started

    telemetry = build_executor_fallback_telemetry(
        phase=phase,
        failed_agent=agent,
        failed_model=_phase_model_for_agent_label(ctx, phase, agent),
        fallback_agent=fallback_agent,
        fallback_model=_model_for_agent_override(fallback_agent),
        output=output,
    )
    runner_mod._write_task_log(
        ctx.task["id"],
        agent,
        f"{phase_name}-tooling-failure",
        append_executor_telemetry_marker(output, telemetry),
        exit_code,
        started,
    )
    message = f"{label} 的 {agent} 工具失败，自动切换到 {fallback_agent} 继续执行"
    echo(f"[yellow]  {message}[/yellow]")
    progress_bus.emit(
        task_id=ctx.task_id_for_events,
        stage=display_phase,
        level="warning",
        event_type="phase_retry",
        message=message,
        extra={
            "round": round_num,
            "round_total": ctx.max_rounds,
            "phase_kind": phase,
            "failed_agent": agent,
            "fallback_agent": fallback_agent,
            "executor_family": telemetry["executor_family"],
            "executor_model": telemetry["executor_model"],
            "fallback_reason": telemetry["fallback_reason"],
            "fallback_path": telemetry["fallback_path"],
            "failed_executor": telemetry["failed_executor"],
            "fallback_executor": telemetry["fallback_executor"],
        },
    )

    fallback_output_path = _make_phase_output_path(
        ctx.output_dir,
        ctx.task["id"],
        round_num,
        f"{phase}-fallback",
    )
    fallback_started = datetime.now()
    fallback_label, fallback_exit_code, fallback_output = runner_mod._run_builtin_phase(
        task=ctx.task,
        project_path=ctx.project_path,
        phase=phase,
        prompt=prompt,
        output_path=fallback_output_path,
        timeout=timeout,
        config_ref=ctx.config_ref,
        display_phase=display_phase,
        silence_timeout_seconds=ctx.silence_timeout,
        agent_override=fallback_agent,
    )
    if fallback_exit_code != 0:
        fallback_output = (
            f"原 agent {agent} 因工具异常失败，已自动切换到 {fallback_label}，但备用 agent 仍失败。\n\n"
            f"【原始工具异常】\n{output}\n\n"
            f"【备用 agent 输出】\n{fallback_output}"
        )
    return fallback_label, fallback_exit_code, fallback_output, fallback_started


def _run_builder_round(
    ctx: _ExecutorContext,
    *,
    round_num: int,
    previous_findings: str,
) -> _PhaseOutcome:
    """Run one builder invocation; persist a task_log row on completion."""
    from codepilot.core import progress_bus

    output_path = _make_phase_output_path(ctx.output_dir, ctx.task["id"], round_num, "builder")
    label = "builder" if round_num == 1 else f"builder (round {round_num}/{ctx.max_rounds})"
    display_phase = _display_phase_name("builder", round_num=round_num, round_total=ctx.max_rounds)
    echo(f"[dim]  阶段: {label}[/dim]")
    progress_bus.emit(
        task_id=ctx.task_id_for_events,
        stage=display_phase,
        event_type="phase_start",
        message=f"启动 {label}",
        extra={"round": round_num, "round_total": ctx.max_rounds, "phase_kind": "builder"},
    )

    prompt = _build_builtin_prompt(
        ctx.task,
        ctx.task_file,
        project_path=ctx.project_path,
        review_round=round_num,
        previous_review_feedback=previous_findings,
        language=_agent_language_for_context(ctx),
    )
    try:
        agent, exit_code, output, started = _run_phase_with_tooling_fallback(
            ctx,
            phase="builder",
            round_num=round_num,
            phase_name="builder" if round_num == 1 else f"builder-r{round_num}",
            label=label,
            display_phase=display_phase,
            prompt=prompt,
            output_path=output_path,
            timeout=3600,
        )
    except Exception as exc:
        progress_bus.emit(
            task_id=ctx.task_id_for_events,
            stage=display_phase,
            level="error",
            event_type="error",
            message=f"{label} 异常终止：{exc}",
            extra={"round": round_num, "round_total": ctx.max_rounds, "phase_kind": "builder"},
        )
        raise
    phase_name = "builder" if round_num == 1 else f"builder-r{round_num}"
    _runner_module()._write_task_log(ctx.task["id"], agent, phase_name, output, exit_code, started)
    progress_bus.emit(
        task_id=ctx.task_id_for_events,
        stage=display_phase,
        level="info" if exit_code == 0 else "error",
        event_type="phase_end" if exit_code == 0 else "error",
        message=(f"{label} 完成" if exit_code == 0 else f"{label} 失败（exit={exit_code}）"),
        extra={
            "round": round_num,
            "round_total": ctx.max_rounds,
            "phase_kind": "builder",
            "agent": agent,
            "exit_code": exit_code,
        },
    )
    return _PhaseOutcome(agent=agent, exit_code=exit_code, output=output, display_phase=display_phase)


def _run_reviewer_round(
    ctx: _ExecutorContext,
    *,
    round_num: int,
    previous_findings: str = "",
) -> _PhaseOutcome:
    """Run one reviewer invocation; persist a task_log row on completion."""
    from codepilot.core import progress_bus

    output_path = _make_phase_output_path(ctx.output_dir, ctx.task["id"], round_num, "review")
    label = "reviewer" if round_num == 1 else f"reviewer (round {round_num}/{ctx.max_rounds})"
    display_phase = _display_phase_name("reviewer", round_num=round_num, round_total=ctx.max_rounds)
    echo(f"[dim]  阶段: {label}[/dim]")
    progress_bus.emit(
        task_id=ctx.task_id_for_events,
        stage=display_phase,
        event_type="phase_start",
        message=f"启动 {label}",
        extra={"round": round_num, "round_total": ctx.max_rounds, "phase_kind": "reviewer"},
    )

    try:
        changed_files = _runner_module()._git_changed_files(ctx.project_path)
    except Exception:
        changed_files = []
    prompt = _build_review_prompt(
        ctx.task,
        review_round=round_num,
        previous_findings=previous_findings,
        changed_files=changed_files,
        language=_agent_language_for_context(ctx),
    )
    try:
        agent, exit_code, output, started = _run_phase_with_tooling_fallback(
            ctx,
            phase="reviewer",
            round_num=round_num,
            phase_name="reviewer" if round_num == 1 else f"reviewer-r{round_num}",
            label=label,
            display_phase=display_phase,
            prompt=prompt,
            output_path=output_path,
            timeout=1800,
        )
    except Exception as exc:
        progress_bus.emit(
            task_id=ctx.task_id_for_events,
            stage=display_phase,
            level="error",
            event_type="error",
            message=f"{label} 异常终止：{exc}",
            extra={"round": round_num, "round_total": ctx.max_rounds, "phase_kind": "reviewer"},
        )
        agent = _expected_phase_agent_label(ctx.task, "reviewer", ctx.config_ref)
        started = datetime.now()
        phase_name = "reviewer" if round_num == 1 else f"reviewer-r{round_num}"
        _runner_module()._write_task_log(ctx.task["id"], agent, phase_name, str(exc), 1, started)
        return _PhaseOutcome(agent=agent, exit_code=1, output=str(exc), display_phase=display_phase)
    phase_name = "reviewer" if round_num == 1 else f"reviewer-r{round_num}"
    _runner_module()._write_task_log(ctx.task["id"], agent, phase_name, output, exit_code, started)
    if exit_code != 0:
        progress_bus.emit(
            task_id=ctx.task_id_for_events,
            stage=display_phase,
            level="error",
            event_type="error",
            message=f"{label} 失败（exit={exit_code}）",
            extra={
                "round": round_num,
                "round_total": ctx.max_rounds,
                "phase_kind": "reviewer",
                "agent": agent,
                "exit_code": exit_code,
            },
        )
    return _PhaseOutcome(agent=agent, exit_code=exit_code, output=output, display_phase=display_phase)


@dataclass
class _BuiltinLoopOutcome:
    """Terminal state produced by the builder/reviewer orchestration loop."""

    status: str
    round_num: int
    builder: _PhaseOutcome
    reviewer: Optional[_PhaseOutcome] = None
    verdict: str = ""
    summary: str = ""
    deterministic_failure: bool = False


def _finalize_executor_success(
    ctx: _ExecutorContext,
    *,
    auto_commit: bool,
    round_num: int,
    builder: _PhaseOutcome,
    reviewer: _PhaseOutcome,
) -> ExecutionResult:
    """After a PASS verdict, optionally commit and build the success summary."""
    runner_mod = _runner_module()
    patch_artifact = collect_git_patch_artifact(ctx.project_path, run_command=runner_mod._run_command)
    commit_sha = (
        runner_mod._git_auto_commit(ctx.project_path, ctx.task["id"], ctx.task["title"])
        if auto_commit
        else ""
    )
    if commit_sha:
        patch_artifact = dict(patch_artifact)
        patch_artifact["committed"] = True
        patch_artifact["result_commit"] = commit_sha
    parts = [f"内置执行器完成(builder={builder.agent}, reviewer={reviewer.agent}, rounds={round_num})"]
    if commit_sha:
        parts.append(f"commit: {commit_sha}")
    parts.append("review: pass")
    return ExecutionResult(
        exit_code=0,
        output=builder.output,
        review_output=reviewer.output,
        summary=" | ".join(parts),
        executor="builtin",
        artifacts={
            "patch": patch_artifact,
            "validation": validation_artifact_from_result(
                exit_code=0,
                executor="builtin",
                output=builder.output,
                review_output=reviewer.output,
            ),
            "review": review_artifact_from_output(reviewer.output),
        },
    )


def _is_reviewer_timeout_error(reviewer: _PhaseOutcome) -> bool:
    """Check whether a failed reviewer outcome was caused by a timeout."""
    output = str(reviewer.output or "")
    return "timed out" in output.lower() or "timeout" in output.lower() or "超时" in output


def _first_nonempty_line(text: str, *, limit: int = 240) -> str:
    """Return a compact single-line detail for task error summaries."""
    for raw_line in str(text or "").splitlines():
        line = " ".join(raw_line.strip().split())
        if line:
            return line[:limit]
    return ""


def _builder_done_review_tool_failure_summary(reviewer: _PhaseOutcome) -> str:
    """Build the terminal summary for reviewer CLI/tooling failures."""
    detail = _first_nonempty_line(reviewer.output or "")
    detail_part = f"：{detail}" if detail else ""
    return (
        f"✅ Builder 已完成但 ❌ Reviewer 工具失败（exit={reviewer.exit_code}）{detail_part}。"
        "可重试 review、切换 reviewer，或人工接受/提交 builder 补丁。"
    )


def _run_builtin_round_loop(ctx: _ExecutorContext) -> _BuiltinLoopOutcome:
    """Run builder/reviewer rounds until success or a terminal failure state."""
    from codepilot.core import progress_bus

    previous_findings = ""
    builder = _PhaseOutcome(agent="", exit_code=0, output="", display_phase="")
    reviewer = _PhaseOutcome(agent="", exit_code=0, output="", display_phase="")

    for round_num in range(1, ctx.max_rounds + 1):
        builder = _run_builder_round(ctx, round_num=round_num, previous_findings=previous_findings)
        if builder.exit_code != 0:
            return _BuiltinLoopOutcome(status="builder_error", round_num=round_num, builder=builder)

        reviewer = _run_reviewer_round(
            ctx,
            round_num=round_num,
            previous_findings=previous_findings,
        )
        if reviewer.exit_code != 0:
            _is_timeout = _is_reviewer_timeout_error(reviewer)
            status = "builder_done_review_timeout" if _is_timeout else "builder_done_review_tool_failure"
            summary = (
                "✅ Builder 已完成但 ❌ Reviewer 超时，可以重试 review、切换 reviewer 或接受 builder 结果"
                if _is_timeout
                else _builder_done_review_tool_failure_summary(reviewer)
            )
            return _BuiltinLoopOutcome(
                status=status,
                round_num=round_num,
                builder=builder,
                reviewer=reviewer,
                summary=summary,
            )

        verdict_model = parse_reviewer_output(reviewer.output or "")
        verdict = verdict_model.verdict
        review_event_extra = _review_verdict_event_extra(verdict_model, round_num=round_num)
        if verdict == "pass":
            progress_bus.emit(
                task_id=ctx.task_id_for_events,
                stage=reviewer.display_phase or _display_phase_name("reviewer", round_num=round_num, round_total=ctx.max_rounds),
                event_type="phase_end",
                message="reviewer 判定 PASS",
                extra={**review_event_extra, "phase_kind": "reviewer", "round_total": ctx.max_rounds},
            )
            return _BuiltinLoopOutcome(
                status="pass",
                round_num=round_num,
                builder=builder,
                reviewer=reviewer,
                verdict=verdict,
            )

        previous_findings = format_findings_for_builder(verdict_model)
        if round_num >= ctx.max_rounds:
            summary = "review 未通过（已用完重做轮次）" if verdict == "fail" else "review 结果不明确（已用完重做轮次）"
            progress_bus.emit(
                task_id=ctx.task_id_for_events,
                stage=reviewer.display_phase or _display_phase_name("reviewer", round_num=round_num, round_total=ctx.max_rounds),
                level="error",
                event_type="error",
                message=summary,
                extra={**review_event_extra, "phase_kind": "reviewer", "round_total": ctx.max_rounds},
            )
            return _BuiltinLoopOutcome(
                status="exhausted",
                round_num=round_num,
                builder=builder,
                reviewer=reviewer,
                verdict=verdict,
                summary=summary,
                deterministic_failure=True,
            )

        progress_bus.emit(
            task_id=ctx.task_id_for_events,
            stage=reviewer.display_phase or _display_phase_name("reviewer", round_num=round_num, round_total=ctx.max_rounds),
            level="warning",
            event_type="phase_end",
            message=f"reviewer 判定 {verdict.upper()}，准备第 {round_num + 1} 轮重做",
            extra={**review_event_extra, "phase_kind": "reviewer", "round_total": ctx.max_rounds},
        )
        echo(
            f"[yellow]  reviewer 判定 {verdict.upper()}，准备第 {round_num + 1} 轮重做，"
            f"让 builder 针对反馈再改一次[/yellow]"
        )

    raise RuntimeError("_run_builtin_round_loop: unreachable fallthrough")


def _map_builtin_loop_outcome(
    ctx: _ExecutorContext,
    outcome: _BuiltinLoopOutcome,
    *,
    auto_commit: bool,
) -> ExecutionResult:
    """Convert orchestration outcome into the public ExecutionResult shape."""
    if outcome.status == "builder_error":
        return ExecutionResult(
            exit_code=outcome.builder.exit_code,
            output=outcome.builder.output,
            executor="builtin",
        )

    if outcome.status == "reviewer_error":
        reviewer_output = outcome.reviewer.output if outcome.reviewer else ""
        reviewer_exit = outcome.reviewer.exit_code if outcome.reviewer else 1
        return ExecutionResult(
            exit_code=reviewer_exit,
            output=outcome.builder.output,
            review_output=reviewer_output,
            summary=outcome.summary or "review 命令执行失败",
            executor="builtin",
        )

    if outcome.status == "builder_done_review_tool_failure":
        reviewer = outcome.reviewer
        reviewer_output = reviewer.output if reviewer else ""
        reviewer_exit = reviewer.exit_code if reviewer else 1
        return ExecutionResult(
            exit_code=reviewer_exit,
            output=outcome.builder.output,
            review_output=reviewer_output,
            summary=outcome.summary
            or (
                _builder_done_review_tool_failure_summary(reviewer)
                if reviewer
                else "✅ Builder 已完成但 ❌ Reviewer 工具失败。可重试 review、切换 reviewer，或人工接受/提交 builder 补丁。"
            ),
            executor="builtin",
        )

    if outcome.status == "builder_done_review_timeout":
        reviewer = outcome.reviewer
        review_output = reviewer.output if reviewer else ""
        return ExecutionResult(
            exit_code=1,
            output=outcome.builder.output,
            review_output=review_output,
            summary=outcome.summary or "✅ Builder 已完成但 ❌ Reviewer 超时",
            executor="builtin",
            deterministic_failure=False,
        )

    if outcome.status == "pass":
        reviewer = outcome.reviewer
        if reviewer is None:
            raise RuntimeError("_map_builtin_loop_outcome: pass outcome missing reviewer payload")
        try:
            return _runner_module()._finalize_executor_success(
                ctx,
                auto_commit=auto_commit,
                round_num=outcome.round_num,
                builder=outcome.builder,
                reviewer=reviewer,
            )
        except Exception as exc:
            summary = (
                "Builder 已完成且 Reviewer PASS，但 finalize/auto-commit 失败: "
                f"{exc}"
            )
            try:
                _runner_module()._write_task_log(
                    ctx.task["id"],
                    "system",
                    "finalize",
                    summary,
                    1,
                    datetime.now(),
                )
            except Exception:
                pass
            return ExecutionResult(
                exit_code=2,
                output=outcome.builder.output,
                review_output=reviewer.output,
                summary=summary,
                executor="builtin",
                post_success_failure=True,
            )

    if outcome.status == "exhausted":
        reviewer = outcome.reviewer
        review_output = reviewer.output if reviewer else ""
        summary = outcome.summary or (
            "review 未通过（已用完重做轮次）"
            if outcome.verdict == "fail"
            else "review 结果不明确（已用完重做轮次）"
        )
        return ExecutionResult(
            exit_code=2,
            output=outcome.builder.output,
            review_output=review_output,
            summary=summary,
            executor="builtin",
            deterministic_failure=outcome.deterministic_failure or outcome.status == "exhausted",
        )

    raise RuntimeError(f"_map_builtin_loop_outcome: unsupported status={outcome.status!r}")


def _run_builtin_executor(
    task: dict,
    project: dict,
    task_file: Path,
    auto_commit: bool = True,
    *,
    max_review_rounds: int = 2,
    execution_path: Path | None = None,
    allow_dirty_resume: bool = False,
) -> ExecutionResult:
    """Execute a task with Codex/Claude CLI, review, and retry on FAIL."""
    project_path = Path(project["path"]).resolve()
    working_path = Path(execution_path).resolve() if execution_path is not None else project_path
    effective_agent_mode = (
        "codex"
        if _runner_module()._builtin_review_requires_git(task.get("agent", "codex"), task=task, project_ref=project)
        else "dual"
    )
    dirty_worktree_policy = _dirty_worktree_policy_for_project(project, project_path)
    preflight_error = _runner_module()._builtin_preflight_error(
        working_path,
        auto_commit,
        effective_agent_mode,
        dirty_worktree_policy=dirty_worktree_policy,
    )
    if allow_dirty_resume and "未提交改动" in preflight_error:
        preflight_error = ""
    if not preflight_error and not allow_dirty_resume:
        preflight_error = _runner_module()._handle_preflight_dirty_worktree(
            working_path,
            project,
            task,
            dirty_worktree_policy,
        )
    if preflight_error:
        raise PreflightSkipError(preflight_error)

    silence_timeout = 0
    try:
        cfg = load_project_config(project)
        silence_timeout = int(getattr(getattr(cfg, "automation", None), "agent_silence_timeout_seconds", 0) or 0)
    except Exception:
        silence_timeout = 0

    ctx = _ExecutorContext(
        task=task,
        project=project,
        project_path=working_path,
        config_ref=project.get("config_file") or project_path,
        output_dir=_runner_module()._builtin_runtime_dir(project),
        task_file=task_file,
        max_rounds=max(1, int(max_review_rounds or 1)),
        task_id_for_events=(int(task.get("id") or 0) or None),
        silence_timeout=silence_timeout,
    )
    loop_outcome = _runner_module()._run_builtin_round_loop(ctx)
    return _runner_module()._map_builtin_loop_outcome(ctx, loop_outcome, auto_commit=auto_commit)
