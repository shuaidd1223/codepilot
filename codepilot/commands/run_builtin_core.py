"""Shared helpers for the built-in executor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from codepilot.storage import database as db
from codepilot.ai_support.service import normalize_agent_name, resolve_dual_phase_agents
from codepilot.core.config import normalize_preflight_dirty_worktree
from codepilot.core.paths import project_storage_root
from codepilot.commands.run_git import (
    _git_current_branch,
    _git_has_changes,
    _git_is_repo,
    _git_local_branch_exists,
)
from codepilot.commands.run_shell import _run_command


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
    deterministic_failure: bool = False
    post_success_failure: bool = False
    artifacts: dict | None = None


def _builtin_runtime_dir(project: dict) -> Path:
    """Store builtin executor artifacts outside the repo to avoid polluting commits."""
    project_path = Path(project["path"]).resolve()
    output_dir = project_storage_root(project, project_path=project_path) / "runs"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _preflight_blocked_detail(error_message: str | None) -> dict | None:
    """If ``error_message`` is a known preflight blockage, return structured info.

    Returns ``{"blocked": True, "reason": str, "suggested_actions": [str]}``
    when the error originates from a dirty-worktree / non-git preflight stop,
    or ``None`` for non-blocking / unknown errors.
    """
    if not error_message:
        return None
    if "未提交改动" in error_message and "stop" in error_message:
        return {
            "blocked": True,
            "reason": "工作区有未提交改动，且预检策略为 stop",
            "suggested_actions": [
                "提交改动并继续: git add -A && git commit -m 'wip'",
                "暂存改动并继续: git stash push --include-untracked -m '工作区暂存'",
                "允许自动提交: 在 AGENTS.toml [automation] 中设置 preflight_dirty_worktree = 'commit'",
                "允许自动暂存: 在 AGENTS.toml [automation] 中设置 preflight_dirty_worktree = 'stash'",
                "使用独立 worktree: 在 AGENTS.toml [automation] 中设置 task_workspace = 'branch' 或 'worktree'",
            ],
        }
    if "Git 仓库" in error_message:
        return {
            "blocked": True,
            "reason": "项目还不是 Git 仓库",
            "suggested_actions": [
                "初始化仓库: git init && git add -A && git commit -m 'init'",
                "改用非 codex reviewer: 在 AGENTS.toml [agents] 中设置 reviewer = 'claude'",
            ],
        }
    return None


def _builtin_preflight_error(
    project_path: Path,
    auto_commit: bool,
    agent_mode: str = "codex",
    *,
    dirty_worktree_policy: str = "stop",
) -> str:
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
    policy = normalize_preflight_dirty_worktree(dirty_worktree_policy)
    if _git_is_repo(project_path) and _git_has_changes(project_path) and policy == "stop":
        return (
            "内置执行器检测到主工作区已有未提交改动。"
            "当前预检策略为 stop，本次跳过执行且不消耗重试次数。"
            "请先提交/暂存现有改动，或在 AGENTS.toml 的 [automation] 中设置 "
            'preflight_dirty_worktree = "commit" / "stash"。'
        )
    return ""


def _git_status_short(project_path: Path) -> str:
    code, output = _run_command(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=project_path,
        timeout=30,
    )
    if code != 0:
        raise RuntimeError(f"git status 失败:\n{output}")
    return output.strip()


def _preflight_record_path(project: dict, task_id: int, action: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return _builtin_runtime_dir(project) / f"task-{task_id}-preflight-{action}-{timestamp}.md"


def _record_preflight_dirty_worktree(
    project: dict,
    task: dict,
    *,
    action: str,
    content: str,
    exit_code: int = 0,
) -> Path:
    task_id = int(task.get("id") or 0)
    record_path = _preflight_record_path(project, task_id, action)
    record_path.write_text(content, encoding="utf-8")
    started_at = datetime.now()
    output = f"{content}\n\n记录文件: {record_path}"
    try:
        db.create_task_log(
            task_id=task_id,
            agent="system",
            phase="preflight",
            output=output,
            exit_code=exit_code,
            started_at=started_at.isoformat(),
            finished_at=datetime.now().isoformat(),
            duration=0,
        )
    except Exception:
        pass
    return record_path


def _commit_preflight_dirty_worktree(
    project_path: Path,
    project: dict,
    task: dict,
    *,
    status: str,
) -> str:
    task_id = int(task.get("id") or 0)
    title = " ".join(str(task.get("title") or "").strip().split())[:60] or "untitled"
    add_code, add_output = _run_command(["git", "add", "-A"], cwd=project_path, timeout=120)
    if add_code != 0:
        return f"预检提交失败：git add 失败:\n{add_output}"

    diff_code, _ = _run_command(["git", "diff", "--cached", "--quiet"], cwd=project_path, timeout=30)
    if diff_code == 0:
        return ""

    subject = f"codepilot preflight: save worktree before task #{task_id}"
    body = "\n".join(
        [
            f"Task: #{task_id} {title}",
            "",
            "Dirty workspace status before commit:",
            status or "(empty)",
        ]
    )
    commit_code, commit_output = _run_command(
        ["git", "commit", "-m", subject, "-m", body],
        cwd=project_path,
        timeout=300,
    )
    if commit_code != 0:
        return f"预检提交失败：git commit 失败:\n{commit_output}"

    sha_code, sha_output = _run_command(["git", "rev-parse", "--short", "HEAD"], cwd=project_path, timeout=30)
    sha = sha_output.strip() if sha_code == 0 else ""
    content = "\n".join(
        [
            "# CodePilot Preflight Commit",
            "",
            f"- Task: #{task_id} {title}",
            f"- Commit: {sha or '(unknown)'}",
            "",
            "## Dirty Workspace Status",
            "",
            "```text",
            status or "(empty)",
            "```",
        ]
    )
    _record_preflight_dirty_worktree(project, task, action="commit", content=content)
    return ""


def _stash_preflight_dirty_worktree(
    project_path: Path,
    project: dict,
    task: dict,
    *,
    status: str,
) -> str:
    task_id = int(task.get("id") or 0)
    title = " ".join(str(task.get("title") or "").strip().split())[:60] or "untitled"
    message = f"codepilot preflight stash before task #{task_id}: {title}"
    stash_code, stash_output = _run_command(
        ["git", "stash", "push", "--include-untracked", "-m", message],
        cwd=project_path,
        timeout=300,
    )
    if stash_code != 0:
        return f"预检 stash 失败:\n{stash_output}"
    if "No local changes to save" in stash_output:
        return ""

    ref_code, ref_output = _run_command(
        ["git", "stash", "list", "-n", "1", "--format=%gd%x09%s"],
        cwd=project_path,
        timeout=30,
    )
    stash_ref = "stash@{0}"
    if ref_code == 0 and ref_output.strip():
        stash_ref = ref_output.strip().split("\t", 1)[0].strip() or stash_ref

    content = "\n".join(
        [
            "# CodePilot Preflight Stash",
            "",
            f"- Task: #{task_id} {title}",
            f"- Stash: {stash_ref}",
            f"- Message: {message}",
            "",
            "## Restore",
            "",
            "```bash",
            f"git stash show -p {stash_ref}",
            f"git stash pop {stash_ref}",
            "```",
            "",
            "## Dirty Workspace Status",
            "",
            "```text",
            status or "(empty)",
            "```",
        ]
    )
    _record_preflight_dirty_worktree(project, task, action="stash", content=content)
    return ""


def _handle_preflight_dirty_worktree(
    project_path: Path,
    project: dict,
    task: dict,
    dirty_worktree_policy: str,
) -> str:
    """Apply configured dirty-worktree policy before builtin execution starts."""
    policy = normalize_preflight_dirty_worktree(dirty_worktree_policy)
    if policy == "stop":
        return ""
    if not _git_is_repo(project_path) or not _git_has_changes(project_path):
        return ""
    try:
        status = _git_status_short(project_path)
    except Exception as exc:
        return f"读取预检工作区状态失败: {exc}"
    if not status:
        return ""
    if policy == "commit":
        return _commit_preflight_dirty_worktree(project_path, project, task, status=status)
    if policy == "stash":
        return _stash_preflight_dirty_worktree(project_path, project, task, status=status)
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
_BRACKET_SECTION_RE = re.compile(r"^【([^】\n]+)】\s*", re.MULTILINE)

_SECTION_ALIASES: dict[str, str] = {
    "唯一目标": "任务目标",
    "任务目标": "任务目标",
    "目标": "任务目标",
    "Task Goal": "任务目标",
    "Goal": "任务目标",
    "验收标准": "验收标准",
    "验收": "验收标准",
    "验证": "验收标准",
    "验证命令": "验收标准",
    "Acceptance Criteria": "验收标准",
    "Builder 职责": "Builder 职责",
    "实施提示": "Builder 职责",
    "Builder Responsibilities": "Builder 职责",
    "In Scope": "Builder 职责",
    "Reviewer 职责": "Reviewer 职责",
    "Reviewer Responsibilities": "Reviewer 职责",
    "Reviewer Checkpoints": "Reviewer 职责",
    "涉及文件": "涉及文件",
    "要动的文件": "涉及文件",
    "文件": "涉及文件",
    "Files In Scope": "涉及文件",
    "Files": "涉及文件",
    "禁区": "禁区",
    "不要做": "禁区",
    "Forbidden": "禁区",
    "Forbidden (Hard Boundary)": "禁区",
    "依赖": "依赖",
    "Dependencies": "依赖",
    "不涉及": "不涉及",
    "Out of Scope": "不涉及",
}


def _extract_task_sections(content: str) -> dict[str, str]:
    """Parse ``task.content`` into a {canonical_section_key: body} dict."""
    if not content:
        return {}

    anchors: list[tuple[int, int, str]] = []
    for match in _SECTION_RE.finditer(content):
        anchors.append((match.start(), match.end(), match.group(1).strip()))
    for match in _BRACKET_SECTION_RE.finditer(content):
        anchors.append((match.start(), match.end(), match.group(1).strip()))
    if not anchors:
        return {}

    anchors.sort(key=lambda item: item[0])

    sections: dict[str, str] = {}
    for idx, (_start, body_start, title) in enumerate(anchors):
        body_end = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(content)
        body = content[body_start:body_end].strip()
        canonical = _SECTION_ALIASES.get(title, title)
        sections.setdefault(canonical, body)
    return sections


def _bullet_lines(body: str) -> list[str]:
    """Return non-empty bullet items from a markdown section body."""
    if not body:
        return []
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line in {"- 待补充", "- 无", "- （待确认）", "- TBD", "- None", "- (TBD)"}:
            continue
        if line.startswith(("- ", "* ")):
            items.append(line[2:].strip())
        elif line[:2].isdigit() and line[2:3] in {".", "、", ")"}:
            items.append(line[3:].strip())
        else:
            items.append(line)
    return [item for item in items if item]


def _collect_project_conventions_snippet(project_path: Path, *, max_chars: int = 800) -> str:
    """Grab a compact conventions snippet for builder prompts."""
    try:
        from codepilot.ai_support.planner_context import _read_project_conventions
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
