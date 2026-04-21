"""Git operations used by the task runner.

Split out from run.py for maintainability. Re-exported by run.py.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

import click

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


def _git_changed_files(project_path: Path) -> list[str]:
    """List tracked+untracked file paths changed in the working tree.

    Used by the reviewer prompt so it can cross-check the builder's edits
    against the task's declared scope. Falls back to an empty list when
    the repo is inaccessible.
    """
    code, output = _run_command(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=project_path,
        timeout=30,
    )
    if code != 0 or not output.strip():
        return []
    files: list[str] = []
    for line in output.splitlines():
        line = line.rstrip()
        if len(line) < 4:
            continue
        # Line format: "XY path" or "XY path -> renamed"
        body = line[3:].strip()
        if " -> " in body:
            body = body.split(" -> ", 1)[1]
        body = body.strip('"')
        if body:
            files.append(body)
    return files


def _slugify_path_part(text: str, max_length: int = 48, fallback: str = "task") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].strip("-")
    return slug or fallback


def _slugify_branch_part(text: str, max_length: int = 48) -> str:
    return _slugify_path_part(text, max_length=max_length, fallback="task")


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


def _resolve_project_worktree_base(project_info: dict, config=None) -> Path:
    project_path = Path(project_info.get("path") or ".").resolve()
    configured = (project_info.get("worktree_base") or "").strip()
    if not configured and config:
        configured = (getattr(config, "worktree_base", "") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = (project_path / candidate).resolve()
        return candidate
    project_name = (project_info.get("name") or project_path.name or "project").strip()
    slug = _slugify_path_part(project_name, max_length=48, fallback="project")
    fingerprint = hashlib.sha1(str(project_path).encode("utf-8")).hexdigest()[:10]
    return Path.home() / ".codepilot" / "worktrees" / f"{slug}-{fingerprint}"


def _task_worktree_path(project_info: dict, *, task_id: int, title: str, config=None) -> Path:
    base_dir = _resolve_project_worktree_base(project_info, config=config)
    dirname = f"task-{task_id}-{_slugify_path_part(title, max_length=48, fallback='task')}"
    return base_dir / dirname


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


def _git_list_worktree_details(project_path: Path) -> list[dict[str, Path | str | None]]:
    code, output = _run_command(["git", "worktree", "list", "--porcelain"], cwd=project_path, timeout=60)
    if code != 0:
        raise RuntimeError(f"列出 git worktree 失败:\n{output}")
    worktrees: list[dict[str, Path | str | None]] = []
    current: dict[str, Path | str | None] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current and current.get("path"):
                worktrees.append(current)
            current = None
            continue
        if line.startswith("worktree "):
            if current and current.get("path"):
                worktrees.append(current)
            raw_path = line[len("worktree ") :].strip()
            current = {"path": Path(raw_path).expanduser().resolve(), "branch": None}
            continue
        if current is None:
            continue
        if line.startswith("branch "):
            raw_branch = line[len("branch ") :].strip()
            branch_prefix = "refs/heads/"
            if raw_branch.startswith(branch_prefix):
                raw_branch = raw_branch[len(branch_prefix) :]
            current["branch"] = raw_branch or None
    if current and current.get("path"):
        worktrees.append(current)
    return worktrees


def _git_list_worktrees(project_path: Path) -> list[Path]:
    return [entry["path"] for entry in _git_list_worktree_details(project_path) if isinstance(entry.get("path"), Path)]


def _git_find_branch_worktrees(project_path: Path, branch: str) -> list[Path]:
    normalized = (branch or "").strip()
    if not normalized:
        return []
    return [
        entry["path"]
        for entry in _git_list_worktree_details(project_path)
        if isinstance(entry.get("path"), Path) and (entry.get("branch") or "").strip() == normalized
    ]


def _git_get_worktree_detail(project_path: Path, worktree_path: Path) -> dict[str, Path | str | None] | None:
    target = Path(worktree_path).expanduser().resolve()
    for detail in _git_list_worktree_details(project_path):
        if detail.get("path") == target:
            return detail
    return None


def _git_worktree_exists(project_path: Path, worktree_path: Path) -> bool:
    return _git_get_worktree_detail(project_path, worktree_path) is not None


def _git_prune_worktrees(project_path: Path) -> None:
    code, output = _run_command(["git", "worktree", "prune"], cwd=project_path, timeout=120)
    if code != 0:
        raise RuntimeError(f"清理 git worktree 元数据失败:\n{output}")


def _git_merge_in_progress(project_path: Path) -> bool:
    code, _ = _run_command(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=project_path, timeout=30)
    return code == 0


def _path_is_empty_directory(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def _git_prepare_task_worktree(
    project_path: Path,
    *,
    task_id: int,
    title: str,
    base_branch: str,
    worktree_path: Path,
) -> tuple[str, Path]:
    if not _git_is_repo(project_path):
        return "", project_path.resolve()
    _git_prune_worktrees(project_path)
    if not _git_local_branch_exists(project_path, base_branch):
        raise RuntimeError(
            f"配置的 base_branch `{base_branch}` 在本地不存在，无法创建任务 worktree。"
            "请先创建该分支，或修正项目配置。"
        )

    target_path = Path(worktree_path).expanduser().resolve()
    task_branch = _task_branch_name(task_id, title)
    target_detail = _git_get_worktree_detail(project_path, target_path)
    if target_detail:
        if target_path == project_path.resolve():
            raise RuntimeError(f"目标 worktree 路径不能指向主工作区: {target_path}")
        registered_branch = (target_detail.get("branch") or "").strip()
        if registered_branch != task_branch:
            branch_label = registered_branch or "detached HEAD"
            raise RuntimeError(
                f"目标 worktree 路径已被其他分支占用: {target_path} ({branch_label})\n"
                "请先手动清理该 worktree 或更换 worktree_base。"
            )
        return task_branch, target_path
    elif target_path.exists() and not _path_is_empty_directory(target_path):
        raise RuntimeError(
            f"目标 worktree 路径已存在且不属于当前仓库: {target_path}\n"
            "请先手动清理该目录或更换 worktree_base。"
        )

    occupying_worktrees = [path for path in _git_find_branch_worktrees(project_path, task_branch) if path != target_path]
    if occupying_worktrees:
        conflict_path = occupying_worktrees[0]
        raise RuntimeError(
            f"任务分支已被其他 worktree 占用: {task_branch} ({conflict_path})\n"
            "请先清理旧 worktree，避免覆盖已有任务上下文。"
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "worktree", "add", "--force"]
    if _git_local_branch_exists(project_path, task_branch):
        cmd.extend([str(target_path), task_branch])
    else:
        cmd.extend(["-b", task_branch, str(target_path), base_branch])

    code, output = _run_command(
        cmd,
        cwd=project_path,
        timeout=300,
    )
    if code != 0:
        raise RuntimeError(f"创建任务 worktree 失败:\n{output}")
    return task_branch, target_path


def _git_cleanup_task_worktree(
    project_path: Path,
    *,
    worktree_path: Path | str | None,
    task_branch: str = "",
    keep_branch: bool = False,
) -> None:
    if not _git_is_repo(project_path) or not worktree_path:
        return
    target_path = Path(worktree_path).expanduser().resolve()
    target_detail = _git_get_worktree_detail(project_path, target_path)
    removed_matching_worktree = False
    if target_detail:
        if target_path == project_path.resolve():
            raise RuntimeError(f"不能移除主工作区: {target_path}")
        registered_branch = (target_detail.get("branch") or "").strip()
        if task_branch and registered_branch != task_branch:
            branch_label = registered_branch or "detached HEAD"
            raise RuntimeError(
                f"拒绝移除不属于任务分支的 worktree: {target_path} ({branch_label})"
            )
        code, output = _run_command(
            ["git", "worktree", "remove", "--force", str(target_path)],
            cwd=project_path,
            timeout=300,
        )
        if code != 0:
            raise RuntimeError(f"移除任务 worktree 失败:\n{output}")
        removed_matching_worktree = True
    _git_prune_worktrees(project_path)
    if _path_is_empty_directory(target_path):
        target_path.rmdir()
    if task_branch and removed_matching_worktree and not keep_branch and _git_local_branch_exists(project_path, task_branch):
        del_code, del_output = _run_command(["git", "branch", "-D", task_branch], cwd=project_path, timeout=60)
        if del_code != 0:
            raise RuntimeError(f"删除任务 worktree 分支失败:\n{del_output}")


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


def _git_merge_task_worktree(
    project_path: Path,
    *,
    task_id: int,
    title: str,
    task_branch: str,
    base_branch: str,
    worktree_path: Path | str,
) -> str:
    """Merge a task-branch worktree back into base_branch on the main project.

    任务执行发生在独立 ``worktree_path``；主工作目录 ``project_path`` 只负责
    保持在 ``base_branch`` 并执行最终 merge。若任务 worktree 仍有未提交改动，
    会先报错并保留现场，避免把未提交结果直接清理掉。
    """
    if not _git_is_repo(project_path) or not task_branch or task_branch == base_branch:
        return ""
    task_worktree = Path(worktree_path).expanduser().resolve()
    if _git_has_changes(task_worktree):
        raise RuntimeError(
            "任务 worktree 存在未提交改动，无法自动合并回 base_branch。"
            "请先在该 worktree 内提交改动，或在本次执行启用 auto-commit。"
        )
    _git_checkout(project_path, base_branch)
    merge_code, merge_output = _run_command(
        ["git", "merge", "--no-ff", "--no-edit", task_branch],
        cwd=project_path,
        timeout=300,
    )
    if merge_code != 0:
        recovery_note = ""
        if _git_merge_in_progress(project_path):
            abort_code, abort_output = _run_command(["git", "merge", "--abort"], cwd=project_path, timeout=120)
            if abort_code != 0:
                recovery_note = f"\n另外，自动执行 `git merge --abort` 失败:\n{abort_output}"
            else:
                recovery_note = "\n已自动执行 `git merge --abort`，主工作区保持在 base_branch。"
        raise RuntimeError(f"合并任务分支失败:\n{merge_output}{recovery_note}")
    safe_title = " ".join((title or "").strip().split())[:60]
    merged_title = safe_title or f"task #{task_id}"
    summary = f"已合并 `{task_branch}` -> `{base_branch}` ({merged_title}) [worktree]"
    try:
        _git_cleanup_task_worktree(
            project_path,
            worktree_path=worktree_path,
            task_branch=task_branch,
            keep_branch=False,
        )
    except Exception as exc:
        cleanup_note = f"已合并成功，但清理任务 worktree 失败: {exc}"
        click.echo(f"  [warn] {cleanup_note}")
        summary = f"{summary} | cleanup: {cleanup_note}"
    return summary


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

