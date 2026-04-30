"""Collector implementations and compatibility exports for inspect signals."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from codepilot.commands.inspect_signal_collectors_code_metrics import collect_code_metrics
from codepilot.commands.inspect_signal_collectors_dependency_health import (
    collect_dependency_health,
)
from codepilot.commands.inspect_signal_collectors_todos import collect_todos
from codepilot.storage import database as db
from codepilot.core.text_decode import decode_subprocess_text


def _run_git(args: list[str], cwd: Path, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=False,
            timeout=timeout,
        )
        return decode_subprocess_text(result.stdout).strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def collect_git_log(project_path: Path, limit: int = 20) -> str:
    log = _run_git(
        ["log", f"-n{limit}", "--pretty=format:%h %s", "--since=7.days"],
        project_path,
    )
    return log or "（近 7 天无提交）"


def collect_failed_tasks(project: str, limit: int = 10) -> str:
    rows = [
        t
        for t in db.list_tasks(project=project)
        if t["status"] in {"failed", "cancelled"}
        and (t.get("source") or "") != "inspector"
    ][:limit]
    if not rows:
        return "（无）"
    lines = []
    for t in rows:
        err = (t.get("error_message") or "").strip().splitlines()
        err_head = err[0] if err else ""
        lines.append(f"#{t['id']} [{t['status']}] {t['title']}  {err_head}")
    return "\n".join(lines)


def _which(cmd: str) -> Optional[str]:
    import shutil

    return shutil.which(cmd)


def collect_ruff(project_path: Path, limit: int = 30) -> str:
    """Run ruff in report-only mode if available."""
    if not _which("ruff"):
        return "（ruff 未安装，跳过）"
    try:
        result = subprocess.run(
            ["ruff", "check", ".", "--output-format", "concise", "--quiet"],
            cwd=str(project_path),
            capture_output=True,
            text=False,
            timeout=60,
        )
        lines = [ln for ln in decode_subprocess_text(result.stdout).splitlines() if ln.strip()]
        if not lines:
            return "（ruff 无发现）"
        return "\n".join(lines[:limit])
    except Exception as exc:
        return f"（ruff 执行失败：{exc}）"


def _summarize_pytest_collect_output(lines: list[str], limit: int = 30) -> str:
    stripped = [ln for ln in lines if ln.strip()]
    if not stripped:
        return "（pytest collect 无输出）"

    def _nodeid_file(line: str) -> str | None:
        if "::" not in line:
            return None
        path_text = line.split("::", 1)[0].replace("\\", "/")
        if not path_text.endswith(".py"):
            return None
        return path_text

    def _is_diagnostic_line(line: str) -> bool:
        if _nodeid_file(line) is not None:
            return False
        lowered = line.lower()
        return (
            lowered.startswith("error")
            or lowered.startswith("warning")
            or " error " in lowered
            or " warning " in lowered
        )

    status_lines = [
        ln
        for ln in stripped
        if _nodeid_file(ln) is None
        and (
            " collected" in ln.lower()
            or " deselected" in ln.lower()
            or " error" in ln.lower()
            or " failed" in ln.lower()
        )
    ]
    warnings_and_errors = [
        ln
        for ln in stripped
        if _is_diagnostic_line(ln)
    ]

    file_counts: dict[str, int] = {}
    for line in stripped:
        path_text = _nodeid_file(line)
        if path_text is None:
            continue
        file_counts[path_text] = file_counts.get(path_text, 0) + 1

    if not file_counts:
        interesting = warnings_and_errors or [
            ln
            for ln in stripped
            if " collected" in ln.lower()
            or " deselected" in ln.lower()
            or "test" in ln.lower()
        ]
        picked = interesting[:limit] if interesting else stripped[-limit:]
        return "\n".join(picked)

    summary: list[str] = []
    if status_lines:
        summary.append(status_lines[-1])
    if warnings_and_errors:
        summary.extend(warnings_and_errors[: max(1, min(5, limit))])

    summary.append(f"{len(file_counts)} test files collected:")
    file_lines = [f"{path} ({count} tests)" for path, count in file_counts.items()]
    visible_count = max(1, limit - len(summary) - 1)
    summary.extend(file_lines[:visible_count])

    omitted = list(file_counts.keys())[visible_count:]
    if omitted:
        summary.append(f"... {len(omitted)} more test files: {', '.join(omitted)}")

    return "\n".join(summary)


def collect_pytest_collect(project_path: Path, limit: int = 30) -> str:
    """Run pytest --collect-only to surface collection errors and test count."""
    if not _which("pytest"):
        return "（pytest 未安装，跳过）"
    if not (project_path / "tests").exists() and not any(project_path.glob("test_*.py")):
        return "（未发现测试目录，跳过）"
    try:
        result = subprocess.run(
            ["pytest", "--collect-only", "-q"],
            cwd=str(project_path),
            capture_output=True,
            text=False,
            timeout=60,
        )
        output = decode_subprocess_text(result.stdout) + decode_subprocess_text(result.stderr)
        lines = [ln for ln in output.splitlines() if ln.strip()]
        return _summarize_pytest_collect_output(lines, limit=limit)
    except Exception as exc:
        return f"（pytest collect 失败：{exc}）"

