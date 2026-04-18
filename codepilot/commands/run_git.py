"""Git operations used by the task runner.

Split out from run.py for maintainability. Re-exported by run.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from codepilot.commands.run_shell import _run_command


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


def _slugify_branch_part(text: str, max_length: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].strip("-")
    return slug or "task"


def _task_branch_name(task_id: int, title: str) -> str:
    return f"feat/task-{task_id}-{_slugify_branch_part(title)}"


def _resolve_project_base_branch(project_info: dict, config=None) -> str:
    configured = (project_info.get("base_branch") or "").strip()
    if configured:
        return configured
    if config:
        fallback = (getattr(config, "base_branch", "") or "").strip()
        if fallback:
            return fallback
    return "dev"


def _git_checkout(project_path: Path, branch: str, *, create_from: Optional[str] = None, reset: bool = False) -> None:
    if create_from:
        cmd = ["git", "checkout", "-B" if reset else "-b", branch, create_from]
    else:
        cmd = ["git", "checkout", branch]
    code, output = _run_command(cmd, cwd=project_path, timeout=120)
    if code != 0:
        raise RuntimeError(f"切换分支 `{branch}` 失败:\n{output}")


def _git_local_branch_exists(project_path: Path, branch: str) -> bool:
    code, _ = _run_command(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=project_path,
        timeout=30,
    )
    return code == 0


def _git_prepare_task_branch(project_path: Path, *, task_id: int, title: str, base_branch: str) -> str:
    if not _git_is_repo(project_path):
        return ""
    if _git_has_changes(project_path):
        raise RuntimeError(
            "检测到当前工作区有未提交改动，无法为任务自动创建独立分支。"
            "请先提交/暂存现有改动后再执行。"
        )
    # 如果配置的 base_branch 在本地不存在，回退到当前分支（不强制切换），避免 pathspec 失败
    if not _git_local_branch_exists(project_path, base_branch):
        current = _git_current_branch(project_path)
        if current:
            base_branch = current
        else:
            return ""
    else:
        _git_checkout(project_path, base_branch)
    task_branch = _task_branch_name(task_id, title)
    _git_checkout(project_path, task_branch, create_from=base_branch, reset=True)
    return task_branch


def _git_merge_task_branch(
    project_path: Path,
    *,
    task_id: int,
    title: str,
    task_branch: str,
    base_branch: str,
) -> str:
    if not _git_is_repo(project_path) or not task_branch or task_branch == base_branch:
        return ""
    if _git_has_changes(project_path):
        raise RuntimeError(
            "任务分支存在未提交改动，无法自动合并回 base_branch。"
            "请先提交改动，或在本次执行启用 auto-commit。"
        )
    _git_checkout(project_path, base_branch)
    merge_code, merge_output = _run_command(
        ["git", "merge", "--no-ff", "--no-edit", task_branch],
        cwd=project_path,
        timeout=300,
    )
    if merge_code != 0:
        raise RuntimeError(f"合并任务分支失败:\n{merge_output}")
    # 合并成功后删除任务分支
    del_code, del_output = _run_command(["git", "branch", "-d", task_branch], cwd=project_path, timeout=30)
    if del_code != 0:
        # 不阻塞，但记录失败原因
        click.echo(f"  [warn] 删除分支 {task_branch} 失败: {del_output.strip()}")
    safe_title = " ".join((title or "").strip().split())[:60]
    merged_title = safe_title or f"task #{task_id}"
    return f"已合并 `{task_branch}` -> `{base_branch}` ({merged_title})"



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

