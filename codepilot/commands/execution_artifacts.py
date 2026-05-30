"""Compact execution artifact summaries for task runs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from codepilot.commands.reviewer_output import parse_reviewer_output


RunCommand = Callable[..., tuple[int, str]]

_MAX_EXCERPT_CHARS = 4000
_MAX_EXCERPT_LINES = 60


def excerpt_text(text: str | None, *, max_chars: int = _MAX_EXCERPT_CHARS) -> str:
    """Return a compact log excerpt without storing a large transcript."""
    raw = str(text or "")
    if not raw:
        return ""
    lines = raw.splitlines()
    if len(lines) > _MAX_EXCERPT_LINES:
        raw = "\n".join(lines[-_MAX_EXCERPT_LINES:])
    return raw[-max_chars:]


def _run_git(project_path: Path, args: list[str], run_command: RunCommand | None) -> tuple[int, str]:
    command = ["git", *args]
    if run_command is not None:
        return run_command(command, cwd=project_path, timeout=30)
    try:
        completed = subprocess.run(
            command,
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)
    return completed.returncode, (completed.stdout or completed.stderr or "")


def _parse_status_files(status_output: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for raw in str(status_output or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        status = (line[:2].strip() or "??") if len(line) >= 2 else "??"
        body = line[3:].strip() if len(line) >= 4 else line.strip()
        if " -> " in body:
            body = body.split(" -> ", 1)[1]
        body = body.strip('"')
        if body:
            files.append({"path": body, "status": status})
    return files


def collect_git_patch_artifact(
    project_path: str | Path,
    *,
    run_command: RunCommand | None = None,
) -> dict[str, Any]:
    """Summarize the current git patch without reading or storing full diff text."""
    root = Path(project_path).resolve()
    rev_code, rev_output = _run_git(root, ["rev-parse", "--is-inside-work-tree"], run_command)
    if rev_code != 0 or "true" not in rev_output.lower():
        return {
            "kind": "patch",
            "status": "unavailable",
            "empty": True,
            "summary": "Git worktree is unavailable; no patch summary was captured.",
            "file_count": 0,
            "files": [],
            "worktree_path": str(root),
        }

    status_code, status_output = _run_git(root, ["status", "--short", "--untracked-files=all"], run_command)
    if status_code != 0:
        return {
            "kind": "patch",
            "status": "unavailable",
            "empty": True,
            "summary": "git status failed; no patch summary was captured.",
            "file_count": 0,
            "files": [],
            "worktree_path": str(root),
            "error_excerpt": excerpt_text(status_output, max_chars=1000),
        }

    files = _parse_status_files(status_output)
    branch_code, branch_output = _run_git(root, ["branch", "--show-current"], run_command)
    commit_code, commit_output = _run_git(root, ["rev-parse", "--short", "HEAD"], run_command)
    stat_code, stat_output = _run_git(root, ["diff", "--stat", "--find-renames"], run_command)
    cached_stat_code, cached_stat_output = _run_git(root, ["diff", "--cached", "--stat", "--find-renames"], run_command)

    if not files:
        return {
            "kind": "patch",
            "status": "empty",
            "empty": True,
            "summary": "No git diff detected.",
            "file_count": 0,
            "files": [],
            "stat": "",
            "cached_stat": "",
            "branch": branch_output.strip() if branch_code == 0 else "",
            "commit": commit_output.strip() if commit_code == 0 else "",
            "worktree_path": str(root),
        }

    shown_files = files[:100]
    summary_names = ", ".join(item["path"] for item in shown_files[:5])
    if len(files) > 5:
        summary_names += f", +{len(files) - 5} more"
    return {
        "kind": "patch",
        "status": "captured",
        "empty": False,
        "summary": f"{len(files)} changed file{'s' if len(files) != 1 else ''}: {summary_names}",
        "file_count": len(files),
        "files": shown_files,
        "truncated_files": max(0, len(files) - len(shown_files)),
        "stat": excerpt_text(stat_output if stat_code == 0 else "", max_chars=2000),
        "cached_stat": excerpt_text(cached_stat_output if cached_stat_code == 0 else "", max_chars=2000),
        "branch": branch_output.strip() if branch_code == 0 else "",
        "commit": commit_output.strip() if commit_code == 0 else "",
        "worktree_path": str(root),
    }


def review_artifact_from_output(review_output: str | None) -> dict[str, Any]:
    raw = str(review_output or "")
    if not raw.strip():
        return {
            "kind": "review",
            "status": "none",
            "verdict": "none",
            "summary": "No reviewer output was recorded.",
            "blockers": [],
            "advisory": [],
            "ac_checks": [],
        }
    parsed = parse_reviewer_output(raw)
    if parsed.source == "empty":
        return {
            "kind": "review",
            "status": "none",
            "verdict": "none",
            "source": parsed.source,
            "summary": "No reviewer verdict was recorded.",
            "blockers": [],
            "advisory": [],
            "ac_checks": [],
        }
    return {
        "kind": "review",
        "status": parsed.verdict,
        "verdict": parsed.verdict,
        "source": parsed.source,
        "summary": f"VERDICT: {parsed.verdict.upper()}",
        "blockers": list(parsed.blockers),
        "advisory": list(parsed.advisory),
        "ac_checks": [dict(item) for item in parsed.ac_checks],
    }


def validation_artifact_from_result(
    *,
    exit_code: int | None,
    executor: str,
    output: str = "",
    review_output: str = "",
    log_path: str = "",
) -> dict[str, Any]:
    ok = exit_code == 0
    return {
        "kind": "validation",
        "status": "passed" if ok else "failed",
        "summary": "Executor completed successfully." if ok else f"Executor failed with exit={exit_code}.",
        "checks": [
            {
                "command": executor or "executor",
                "exit_code": exit_code,
                "ok": ok,
                "output_excerpt": excerpt_text(output or review_output),
                "review_excerpt": excerpt_text(review_output),
                "log_path": log_path,
            }
        ],
    }


def validation_artifact_from_verification(verification: list[dict[str, Any]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for item in verification:
        if not isinstance(item, dict):
            continue
        checks.append(
            {
                "command": str(item.get("command") or ""),
                "exit_code": item.get("exit_code"),
                "ok": item.get("ok"),
                "stdout_excerpt": excerpt_text(item.get("stdout") or ""),
                "stderr_excerpt": excerpt_text(item.get("stderr") or ""),
                "timed_out": bool(item.get("timed_out")),
            }
        )
    if not checks:
        status = "not_run"
        summary = "No verification command was configured."
    elif all(item.get("ok") is True for item in checks):
        status = "passed"
        summary = f"{len(checks)}/{len(checks)} verification checks passed."
    else:
        status = "failed"
        passed = sum(1 for item in checks if item.get("ok") is True)
        summary = f"{passed}/{len(checks)} verification checks passed."
    return {
        "kind": "validation",
        "status": status,
        "summary": summary,
        "checks": checks,
    }
