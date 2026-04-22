"""Built-in executor phase orchestration for queued tasks.

This module owns the builder/reviewer prompt construction, concrete phase
execution, review verdict parsing, and retry loop. ``run.py`` re-exports these
helpers so existing imports and tests keep working while the CLI runner stays
focused on queue orchestration.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from codepilot import db
from codepilot.ai import (
    _get_node_modules_path,
    normalize_agent_name,
    resolve_dual_phase_agents,
)
from codepilot.config import load_project_config
from codepilot.output import echo
from codepilot.paths import project_storage_root
from codepilot.prompts import load_prompt as _load_prompt
from codepilot.commands.run_shell import PreflightSkipError
from codepilot.commands.run_git import (
    _git_has_changes,
    _git_is_repo,
    _git_local_branch_exists,
    _git_current_branch,
)


def _runner_module():
    """Late-bind through ``codepilot.commands.run`` for legacy monkeypatches."""
    from codepilot.commands import run as _run_mod

    return _run_mod


@dataclass
class ExecutionResult:
    """Normalized task execution result."""

    exit_code: int
    output: str = ""
    review_output: str = ""
    summary: str = ""
    executor: str = "dispatch"
    # 标记 "同样的任务再跑一遍也会失败" 的确定性失败。例如 reviewer 连续多轮给出 FAIL。
    # 任务级 retry 只应覆盖瞬时失败（crash/timeout），这里为 True 时直接走 _mark_task_failed。
    deterministic_failure: bool = False


def _builtin_runtime_dir(project: dict) -> Path:
    """Store builtin executor artifacts outside the repo to avoid polluting commits."""
    project_path = Path(project["path"]).resolve()
    output_dir = project_storage_root(project, project_path=project_path) / "runs"
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
            "内置执行器检测到主工作区已有未提交改动。"
            "为避免 run 结束后回合并到 base_branch 时卡住，本次跳过执行且不消耗重试次数。"
            "请先提交/暂存现有改动，或改用 --no-auto-commit 再执行。"
        )
    return ""


def _builtin_base_branch_lock_error(project_path: Path, base_branch: str) -> str:
    """Ensure the main worktree stays pinned to ``base_branch`` during builtin runs."""
    if not _git_is_repo(project_path):
        return ""
    if not _git_local_branch_exists(project_path, base_branch):
        return ""
    current_branch = _git_current_branch(project_path)
    if current_branch == base_branch:
        return ""
    return (
        f"主工作区当前位于 `{current_branch}`，run 前请先切回 base_branch `{base_branch}`。"
        "独立 worktree 执行要求主目录固定在 base_branch。"
    )


def _task_phase_override(task: Optional[dict], key: str) -> Optional[str]:
    """Return a normalized task-level builder/reviewer override when present."""
    if not task:
        return None
    value = task.get(key)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = normalize_agent_name(text)
    if normalized == "dual":
        raise RuntimeError("dual 模式的任务级 builder/reviewer 不能再配置为 dual。")
    return normalized


def _resolve_dual_phase_agents_for_task(task: Optional[dict], project_ref: str | Path | dict | None) -> tuple[str, str]:
    """Resolve the effective dual builder/reviewer pair for one task."""
    config_ref = project_ref
    if isinstance(project_ref, dict):
        config_ref = project_ref.get("config_file") or project_ref.get("path")
    return resolve_dual_phase_agents(
        config_ref,
        builder=_task_phase_override(task, "builder"),
        reviewer=_task_phase_override(task, "reviewer"),
    )


def _builtin_review_requires_git(
    agent_mode: str,
    *,
    task: Optional[dict] = None,
    project_ref: str | Path | dict | None = None,
) -> bool:
    """Return whether the effective reviewer uses Codex review."""
    normalized = normalize_agent_name(agent_mode or "dual")
    if normalized == "dual":
        _, reviewer_agent = _resolve_dual_phase_agents_for_task(task, project_ref)
        return reviewer_agent == "codex"
    return normalized == "codex"




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
# 也支持 `【xxx】` 开头的段落（整行单独一个【xxx】、或行首的【xxx】后紧跟正文）
_BRACKET_SECTION_RE = re.compile(r"^【([^】\n]+)】\s*", re.MULTILINE)

# 把常见中文 section 标题映射到执行器内部用的 key，保证新老任务格式都能识别
_SECTION_ALIASES: dict[str, str] = {
    "唯一目标": "任务目标",
    "任务目标": "任务目标",
    "目标": "任务目标",
    "验收标准": "验收标准",
    "验收": "验收标准",
    "验证": "验收标准",
    "验证命令": "验收标准",
    "Builder 职责": "Builder 职责",
    "实施提示": "Builder 职责",
    "Reviewer 职责": "Reviewer 职责",
    "涉及文件": "涉及文件",
    "要动的文件": "涉及文件",
    "文件": "涉及文件",
    "禁区": "禁区",
    "不要做": "禁区",
    "依赖": "依赖",
    "不涉及": "不涉及",
}


def _extract_task_sections(content: str) -> dict[str, str]:
    """Parse ``task.content`` into a {canonical_section_key: body} dict.

    Supports both legacy ``## SectionName`` markdown headings and the
    ``【SectionName】`` Chinese-bracket style that hand-written tasks tend to
    use. Aliases are normalized via ``_SECTION_ALIASES`` so the downstream
    prompt builders can rely on a small canonical set of keys.
    """
    if not content:
        return {}

    # Collect every (start, end_of_marker, title) anchor, from both styles.
    anchors: list[tuple[int, int, str]] = []
    for match in _SECTION_RE.finditer(content):
        anchors.append((match.start(), match.end(), match.group(1).strip()))
    for match in _BRACKET_SECTION_RE.finditer(content):
        anchors.append((match.start(), match.end(), match.group(1).strip()))
    if not anchors:
        return {}

    anchors.sort(key=lambda a: a[0])

    sections: dict[str, str] = {}
    for idx, (_start, body_start, title) in enumerate(anchors):
        body_end = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(content)
        body = content[body_start:body_end].strip()
        canonical = _SECTION_ALIASES.get(title, title)
        # 同一个 canonical key 多段出现时保留第一段即可（后续段一般是补充说明）。
        sections.setdefault(canonical, body)
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
    forbidden = (sections.get("禁区") or "").strip()
    dependencies = (sections.get("依赖") or "").strip()
    not_in_scope = (sections.get("不涉及") or "").strip()

    lines: list[str] = []
    if review_round <= 1:
        lines.append(f"你正在执行排队任务 #{task['id']}：{task['title']}")
    else:
        lines.append(
            f"这是任务 #{task['id']} 「{task['title']}」的第 {review_round} 轮重做。"
            " 上一轮 reviewer 发现了阻塞问题，请针对性修复。"
        )
    lines.append("")

    # 严格模式前缀：抑制 builder "过度工程化" 的倾向。
    lines.append("【严格模式（必须遵守）】")
    lines.append("1. 只修改任务正文中明确列出的新建/追加/修改文件，不碰其它文件。")
    lines.append("2. 任务正文提供了代码骨架时，按骨架落地；不要新增字段/列/函数/导入/依赖。")
    lines.append("3. 不要做 reviewer 没要求的 '工程最佳实践' 扩展（如复合外键、跨币种、多账户审计等）。")
    lines.append("4. 任务没有要求跑迁移 / 拉依赖 / 启服务时，不要执行。")
    lines.append("5. 最小 diff：删代码仅限任务明确声明；保留现有 import、格式、缩进。")
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

    if forbidden:
        lines.append("【禁区（绝对不要碰的文件/模块）】")
        lines.append(forbidden)
        lines.append("")

    if dependencies:
        lines.append("【前置任务（已完成，直接使用其产出，不要重复实现）】")
        lines.append(dependencies)
        lines.append("")

    if not_in_scope:
        lines.append("【本任务不涉及（留给其它任务，不要提前做）】")
        lines.append(not_in_scope)
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
    previous_findings: str = "",
    changed_files: list[str] | None = None,
) -> str:
    """Compose the Reviewer prompt with acceptance-criteria-driven checklist."""
    sections = _extract_task_sections(task.get("content") or "")
    acceptance = _bullet_lines(sections.get("验收标准") or "")
    reviewer_notes = _bullet_lines(sections.get("Reviewer 职责") or "")
    goal = (sections.get("任务目标") or "").strip()
    forbidden = (sections.get("禁区") or "").strip()
    not_in_scope = (sections.get("不涉及") or "").strip()

    lines = [
        f"请审查当前仓库中为任务 #{task['id']} `{task['title']}` 产生的未提交改动。",
    ]

    if changed_files:
        lines.append("")
        lines.append("【本次 builder 实际改动的文件】")
        for path in changed_files[:40]:
            lines.append(f"  - {path}")
        if len(changed_files) > 40:
            lines.append(f"  - ... 共 {len(changed_files)} 个文件（截断显示前 40）")
        lines.append(
            "判定守则：如果任何一个文件不在任务【唯一目标】/【新建文件】/【追加内容】"
            "声明的路径里，即视为**越界修改**，必须判 FAIL 并在 '需要修复的点' "
            "里要求 builder 回滚那些越界改动。"
        )
    if review_round > 1:
        lines.append(
            f"（这是第 {review_round} 轮审查。根据 reviewer_rules 的硬约束，"
            "本轮你只能复核上一轮已经提出过的阻塞点是否修复；"
            "任何新发现的问题都放到「非阻塞观察」里，不计入 FAIL 理由。）"
        )

    if goal:
        lines.append("")
        lines.append("【任务目标（审查对齐这一点，不要扩展范围）】")
        lines.append(goal)

    if forbidden:
        lines.append("")
        lines.append("【任务声明的禁区（若 builder 触碰则记为 FAIL）】")
        lines.append(forbidden)

    if acceptance:
        lines.append("")
        lines.append("【必须逐条核对的验收标准】")
        for i, item in enumerate(acceptance, 1):
            lines.append(f"  {i}. {item}")
        lines.append(
            "对每一条，明确指出: 通过 / 未通过 / 无法判断，并说明理由（看了哪些文件或命令输出）。"
        )
    else:
        # 没有显式验收标准时，只用"任务目标是否达成"作为唯一判据。
        lines.append("")
        lines.append(
            "【没有显式验收标准】请仅按【任务目标】判断 builder 交付是否完成；"
            "不要补 reviewer 自己的额外要求、架构完整性、测试覆盖率等；"
            "任务文本之外的任何顾虑一律进「非阻塞观察」。"
        )

    if reviewer_notes:
        lines.append("")
        lines.append("【补充检查项（来自规划器的 reviewer 提示）】")
        for item in reviewer_notes:
            lines.append(f"  - {item}")

    if review_round > 1 and previous_findings.strip():
        lines.append("")
        lines.append("【上一轮给 builder 的阻塞意见（本轮只复核这些是否已修复）】")
        lines.append(previous_findings.strip())

    lines.append("")
    lines.append(_load_prompt("reviewer_rules").rstrip())

    # 硬性收尾指令：强制 reviewer 产出 VERDICT 行，避免回退逻辑误判。
    lines.append("")
    lines.append("【输出硬性要求】")
    lines.append(
        "你的回复最后一行必须是 `VERDICT: PASS` 或 `VERDICT: FAIL`，单独一行，"
        "不要写任何其它字符，否则调度器会把本轮当成无效审查。"
    )
    return "\n".join(lines)


def _extract_review_verdict(review_output: str, reviewer_agent: str = "") -> str:
    """Parse reviewer output into pass/fail/unknown.

    Precedence:
    1. Explicit ``VERDICT: PASS|FAIL`` line (required by reviewer_rules.md).
    2. Strong FAIL anchors: a ``需要修复的点``/``需要修复``/``需要处理`` section,
       or any bullet under it. These match the structured output template.
    3. If none of the above match, default to PASS on non-empty output.

    The previous fallback treated any ``- [PX]`` bullet as FAIL, which mis-
    fired on non-blocking observations (allowed by reviewer_rules) and led
    to spurious retries. We no longer do that.
    """
    verdict_pattern = re.compile(r"VERDICT\s*:\s*(PASS|FAIL)\b", re.IGNORECASE)
    for line in reversed(review_output.splitlines()):
        match = verdict_pattern.search(line)
        if match:
            return match.group(1).lower()

    if not review_output.strip():
        return "unknown"

    # Strong FAIL anchor: the reviewer explicitly opens a "needs fix" section.
    if re.search(r"(?mi)^\s*(需要修复的点|需要修复|需要处理|修复建议)\s*[:：]?\s*$", review_output):
        return "fail"

    # Strong PASS anchor: every AC line is marked PASS / N/A, no explicit
    # blocker section was opened. Matches the shape from reviewer_rules.md.
    ac_lines = re.findall(r"(?m)^\s*AC\s*#\d+\s*[:：]\s*(PASS|FAIL|N/A)", review_output, re.IGNORECASE)
    if ac_lines and all(v.lower() in {"pass", "n/a"} for v in ac_lines):
        return "pass"

    # Default: non-empty output without an explicit FAIL marker is treated as
    # PASS, aligning with reviewer_rules' "测试通过是强 PASS 信号" spirit.
    return "pass"


def _resolve_builtin_single_agent(agent_mode: str) -> tuple[str, Optional[str]]:
    """Resolve one concrete agent into a builtin CLI runner + optional model."""
    normalized = normalize_agent_name(agent_mode or "codex")

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
) -> tuple[str, int, str]:
    """Execute one builtin phase with the requested agent.

    ``phase`` drives CLI behavior (``builder`` vs ``reviewer``). ``display_phase``
    is what gets persisted on ``tasks.run_phase`` for the dashboard — use it to
    surface round info like ``"builder r2/4"`` without breaking the phase
    dispatch checks below.
    """
    # stub 注入钩子：e2e 测试可通过 ai._phase_stub 替换真实 CLI 调用
    from codepilot import ai as _ai_hook
    if _ai_hook._phase_stub is not None:
        return _ai_hook._phase_stub(task=task, project_path=project_path, phase=phase, prompt=prompt)

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
class _BuiltinLoopOutcome:
    """Terminal state produced by the builder/reviewer orchestration loop."""

    status: str  # pass | builder_error | reviewer_error | exhausted
    round_num: int
    builder: _PhaseOutcome
    reviewer: Optional[_PhaseOutcome] = None
    verdict: str = ""
    summary: str = ""
    deterministic_failure: bool = False


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
    silence_timeout: int = 0  # seconds; 0 disables the silence detector


def _make_phase_output_path(output_dir: Path, task_id: int, round_num: int, kind: str) -> Path:
    """Reserve a unique file path under ``output_dir`` for a phase's output.

    We use ``mkstemp`` for uniqueness, then delete it immediately because some
    CLI providers (codex/claude) refuse to overwrite an existing ``-o`` file.
    """
    fd, raw = tempfile.mkstemp(
        prefix=f"task-{task_id}-{kind}-r{round_num}-",
        suffix=".md",
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
    display_phase = (
        "builder"
        if round_num == 1
        else f"builder r{round_num}/{ctx.max_rounds}"
    )
    agent, exit_code, output = _runner_module()._run_builtin_phase(
        task=ctx.task,
        project_path=ctx.project_path,
        phase="builder",
        prompt=prompt,
        output_path=output_path,
        timeout=3600,
        config_ref=ctx.config_ref,
        display_phase=display_phase,
        silence_timeout_seconds=ctx.silence_timeout,
    )
    phase_name = "builder" if round_num == 1 else f"builder-r{round_num}"
    _runner_module()._write_task_log(ctx.task["id"], agent, phase_name, output, exit_code, started)
    return _PhaseOutcome(agent=agent, exit_code=exit_code, output=output)


def _run_reviewer_round(
    ctx: _ExecutorContext,
    *,
    round_num: int,
    previous_findings: str = "",
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
    # 列出 builder 在 worktree 里实际改动的文件，提供给 reviewer 做越界守卫。
    try:
        changed_files = _runner_module()._git_changed_files(ctx.project_path)
    except Exception:
        changed_files = []
    prompt = _build_review_prompt(
        ctx.task,
        review_round=round_num,
        previous_findings=previous_findings,
        changed_files=changed_files,
    )
    display_phase = (
        "reviewer"
        if round_num == 1
        else f"reviewer r{round_num}/{ctx.max_rounds}"
    )
    agent, exit_code, output = _runner_module()._run_builtin_phase(
        task=ctx.task,
        project_path=ctx.project_path,
        phase="reviewer",
        prompt=prompt,
        output_path=output_path,
        timeout=1800,
        config_ref=ctx.config_ref,
        display_phase=display_phase,
        silence_timeout_seconds=ctx.silence_timeout,
    )
    phase_name = "reviewer" if round_num == 1 else f"reviewer-r{round_num}"
    _runner_module()._write_task_log(ctx.task["id"], agent, phase_name, output, exit_code, started)
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
        _runner_module()._git_auto_commit(ctx.project_path, ctx.task["id"], ctx.task["title"])
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


def _run_builtin_round_loop(ctx: _ExecutorContext) -> _BuiltinLoopOutcome:
    """Run builder/reviewer rounds until success or a terminal failure state."""
    from codepilot import progress_bus

    previous_findings = ""
    builder = _PhaseOutcome(agent="", exit_code=0, output="")
    reviewer = _PhaseOutcome(agent="", exit_code=0, output="")

    for round_num in range(1, ctx.max_rounds + 1):
        builder = _run_builder_round(ctx, round_num=round_num, previous_findings=previous_findings)
        if builder.exit_code != 0:
            return _BuiltinLoopOutcome(
                status="builder_error",
                round_num=round_num,
                builder=builder,
            )

        reviewer = _run_reviewer_round(
            ctx,
            round_num=round_num,
            previous_findings=previous_findings,
        )
        if reviewer.exit_code != 0:
            return _BuiltinLoopOutcome(
                status="reviewer_error",
                round_num=round_num,
                builder=builder,
                reviewer=reviewer,
                summary="review 命令执行失败",
            )

        verdict = _runner_module()._extract_review_verdict(reviewer.output, reviewer.agent)
        if verdict == "pass":
            progress_bus.emit(
                task_id=ctx.task_id_for_events,
                stage="reviewer",
                message="reviewer 判定 PASS",
                extra={"round": round_num, "verdict": "pass"},
            )
            return _BuiltinLoopOutcome(
                status="pass",
                round_num=round_num,
                builder=builder,
                reviewer=reviewer,
                verdict=verdict,
            )

        previous_findings = _runner_module()._extract_reviewer_findings(reviewer.output)
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
            stage="reviewer",
            level="warning",
            message=f"reviewer 判定 {verdict.upper()}，准备第 {round_num + 1} 轮重做",
            extra={"round": round_num, "verdict": verdict},
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

    if outcome.status == "pass":
        reviewer = outcome.reviewer
        if reviewer is None:
            raise RuntimeError("_map_builtin_loop_outcome: pass outcome missing reviewer payload")
        return _runner_module()._finalize_executor_success(
            ctx,
            auto_commit=auto_commit,
            round_num=outcome.round_num,
            builder=outcome.builder,
            reviewer=reviewer,
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
) -> ExecutionResult:
    """Execute a task with Codex/Claude CLI, review, and retry on FAIL.

    The builder and reviewer form a short loop: a FAIL verdict (or an unclear
    one) feeds the reviewer's findings back into the builder for another pass,
    up to ``max_review_rounds`` attempts. All the single-round mechanics live
    in ``_run_builder_round`` / ``_run_reviewer_round``; this function only
    owns the loop, the verdict decision, and the final commit/summary step.
    """
    project_path = Path(project["path"]).resolve()
    working_path = Path(execution_path).resolve() if execution_path is not None else project_path
    effective_agent_mode = (
        "codex"
        if _runner_module()._builtin_review_requires_git(task.get("agent", "codex"), task=task, project_ref=project)
        else "dual"
    )
    preflight_error = _runner_module()._builtin_preflight_error(working_path, auto_commit, effective_agent_mode)
    if preflight_error:
        # 用 PreflightSkipError 而不是 RuntimeError，run 循环会把任务直接送回
        # backlog，不 bump retry_count。之前用 RuntimeError 会被 generic
        # exception 分支当成真失败处理，导致几次偶发脏工作区就把任务打 failed。
        raise PreflightSkipError(preflight_error)

    # Load silence-timeout from the project's automation config so the
    # main loop can kill wedged agents before the 1h wall-time timeout.
    silence_timeout = 0
    try:
        _cfg = load_project_config(project)
        silence_timeout = int(getattr(getattr(_cfg, "automation", None), "agent_silence_timeout_seconds", 0) or 0)
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

