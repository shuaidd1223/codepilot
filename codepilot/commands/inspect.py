"""Periodic codebase inspection: surface improvement candidates and enqueue them."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tokenize
from pathlib import Path
from typing import Optional

import click

from codepilot import db
from codepilot.ai import (
    API_PROVIDERS,
    _run_api_provider,
    _run_claude_schema_prompt,
    _run_codex_schema_prompt,
    normalize_agent_name,
)
from codepilot.commands.add import _resolve_project_strict
from codepilot.config import load_project_config, resolve_planner
from codepilot.output import echo
from codepilot.paths import _slugify_project_name, global_storage_root
from codepilot.runtime import is_process_alive, stop_process_tree

INSPECT_STATE_DIR = global_storage_root() / "inspect"


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _service_log_path(project: str) -> Path:
    root = INSPECT_STATE_DIR / _slugify_project_name(project)
    return root / "inspect.log"


def inspect_service_status(project: str) -> dict:
    state = db.get_service_state("inspect", project)
    meta = state.get("meta") if state and isinstance(state.get("meta"), dict) else {}
    try:
        pid = int(state.get("pid") or 0) if state else 0
    except Exception:
        pid = 0
    running = bool(pid and is_process_alive(pid))
    return {
        "running": running,
        "pid": pid if running else 0,
        "project": project,
        "started_at": meta.get("started_at") or "",
        "log": str(state.get("log_path") or _service_log_path(project)) if state else str(_service_log_path(project)),
    }


def _cleanup_inspect_files(project: str) -> None:
    db.clear_service_state("inspect", project)


def _write_inspect_meta(project: str, pid: int, *, interval: int, planner: str, agent: str) -> None:
    payload = {
        "pid": int(pid),
        "project": project,
        "interval": int(interval),
        "planner": planner,
        "agent": agent,
        "started_at": _now_iso(),
    }
    db.upsert_service_state(
        "inspect",
        project,
        pid=int(pid),
        status="running",
        log_path=str(_service_log_path(project)),
        heartbeat_at=_now_iso(),
        meta=payload,
    )


def _spawn_detached_inspect(
    project: str,
    *,
    max_new: int | None,
    dry_run: bool,
    agent: str,
    planner: str | None,
    interval: int | None,
) -> subprocess.Popen:
    log_file = _service_log_path(project)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_file, "ab")
    try:
        log_fp.write(f"\n--- start {_now_iso()} project={project} ---\n".encode("utf-8"))
        log_fp.flush()
    except Exception:
        pass
    cmd = [
        sys.executable,
        "-m",
        "codepilot",
        "inspect",
        "--project",
        project,
        "--foreground",
        "--agent",
        agent,
    ]
    if max_new is not None:
        cmd.extend(["--max", str(max_new)])
    if dry_run:
        cmd.append("--dry-run")
    if planner:
        cmd.extend(["--planner", planner])
    if interval is not None:
        cmd.extend(["--interval", str(interval)])
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fp,
        "stderr": log_fp,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        popen_kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **popen_kwargs)


def start_inspect_service(
    project: str,
    *,
    max_new: int | None = None,
    dry_run: bool = False,
    agent: str = "codex",
    planner: str | None = None,
    interval: int | None = None,
) -> dict:
    if not project:
        raise RuntimeError("启动巡检必须指定项目。")
    existing = inspect_service_status(project)
    if existing["running"]:
        existing["started"] = False
        return existing
    _cleanup_inspect_files(project)
    proc = _spawn_detached_inspect(
        project,
        max_new=max_new,
        dry_run=dry_run,
        agent=agent,
        planner=planner,
        interval=interval,
    )
    time.sleep(0.8)
    if proc.poll() is not None:
        tail = ""
        try:
            tail = _service_log_path(project).read_text(encoding="utf-8", errors="replace")[-1500:]
        except Exception:
            pass
        raise RuntimeError(f"巡检启动后立即退出（exit={proc.returncode}）\n{tail}")
    proj = db.get_project(project)
    cfg = load_project_config(Path(proj["path"])) if proj else None
    effective_interval = interval if interval is not None else int(getattr(getattr(cfg, "inspect", None), "interval_seconds", 1800) or 1800)
    effective_planner = planner or (resolve_planner(cfg, "inspect") if cfg else "codex")
    _write_inspect_meta(project, proc.pid, interval=effective_interval, planner=effective_planner, agent=agent)
    return {"running": True, "started": True, "pid": proc.pid, "project": project, "log": str(_service_log_path(project))}


def stop_inspect_service(project: str) -> dict:
    if not project:
        raise RuntimeError("停止巡检必须指定项目。")
    status = inspect_service_status(project)
    if not status["running"]:
        _cleanup_inspect_files(project)
        return {"stopped": False, "pids": []}
    pid = int(status["pid"])
    db.upsert_service_state(
        "inspect",
        project,
        pid=pid,
        status="stopping",
        log_path=str(_service_log_path(project)),
        heartbeat_at=_now_iso(),
        meta={"project": project, "pid": pid, "stop_requested_at": _now_iso()},
    )
    stop_process_tree(pid, wait_seconds=5)
    if is_process_alive(pid):
        raise RuntimeError(f"无法停止巡检 PID={pid}")
    _cleanup_inspect_files(project)
    return {"stopped": True, "pids": [pid]}

INSPECT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
                    "rationale": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["refactor", "bug", "test", "docs", "perf", "chore"],
                    },
                },
                # OpenAI strict structured-output: every object needs
                # additionalProperties=false AND ALL properties in `required`.
                "required": ["title", "goal", "priority", "rationale", "kind"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

INSPECT_PROMPT = """你是当前项目的资深巡检工程师。基于下列信号，挑出 0~{max_tasks} 个真正值得做的改进项，形成结构化候选任务。

硬性要求：
- 只输出确实有价值的，可以为 0 条。宁缺毋滥。
- 每条 title 不超过 40 个中文字符，goal 两三句讲清楚"做什么 + 为什么"。
- 避免建"补一下文档/加日志"这类泛泛的东西，除非信号里直接指向了具体位置。
- 不要重复下面"已存在任务"里的内容。

## 已存在任务（backlog / in-progress）
{existing_titles}

## 信号 1：最近 git 提交
{git_log}

## 信号 2：最近失败或取消的任务
{failed_tasks}

## 信号 3：代码里的 TODO/FIXME/XXX
{todos}

## 信号 4：ruff lint 报告
{ruff}

## 信号 5：pytest --collect-only 摘要
{pytest}

## 信号 6：依赖健康线索
{deps}

请以下列 JSON 返回：{{"candidates": [{{"title": "...", "goal": "...", "priority": "P3", "rationale": "...", "kind": "refactor"}}]}}
如果没有值得提的改进项，返回 {{"candidates": []}}。
"""


def _run_git(args: list[str], cwd: Path, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
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
    ][:limit]
    if not rows:
        return "（无）"
    lines = []
    for t in rows:
        err = (t.get("error_message") or "").strip().splitlines()
        err_head = err[0] if err else ""
        lines.append(f"#{t['id']} [{t['status']}] {t['title']}  {err_head}")
    return "\n".join(lines)


_TODO_RE = re.compile(
    r"\b(?:TODO|FIXME)\b[:：]?\s*.{0,120}|(?<![.`\[\(])\bXXX\b(?:[:：]|\s+).{0,120}",
    re.IGNORECASE,
)
_MD_TODO_RE = re.compile(
    r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)?(?:\[[ xX]\]\s*)?"
    r"(?:TODO|FIXME)\b[:：]?\s*.{0,120}|"
    r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)?(?:\[[ xX]\]\s*)?(?<![.`\[\(])XXX\b(?:[:：]|\s+).{0,120}",
    re.IGNORECASE,
)
_SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".md"}
_COMMENT_MARKERS_BY_EXT = {
    ".py": ("#",),
    ".js": ("//", "/*", "*"),
    ".jsx": ("//", "/*", "*"),
    ".ts": ("//", "/*", "*"),
    ".tsx": ("//", "/*", "*"),
    ".go": ("//", "/*", "*"),
    ".rs": ("//", "/*", "*"),
    ".java": ("//", "/*", "*"),
    ".kt": ("//", "/*", "*"),
}


def _find_unquoted_marker(line: str, markers: tuple[str, ...]) -> int:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        for marker in markers:
            if line.startswith(marker, index):
                return index
    return -1


def _todo_match_in_code_comment(path: Path, line: str) -> re.Match[str] | None:
    """Return TODO-like markers only when they appear in source comments."""
    match = _TODO_RE.search(line)
    if not match:
        return None

    markers = _COMMENT_MARKERS_BY_EXT.get(path.suffix)
    if not markers:
        return None
    todo_pos = match.start()
    marker_pos = _find_unquoted_marker(line, markers)
    return match if marker_pos != -1 and marker_pos <= todo_pos else None


def _todo_match_in_markdown(line: str, in_fenced_block: bool) -> re.Match[str] | None:
    """Keep documentation TODOs intentional and ignore prose/code examples."""
    if in_fenced_block:
        return None
    stripped = line.strip()
    if stripped.startswith("<!--") and "-->" in stripped:
        return _TODO_RE.search(stripped)
    return _MD_TODO_RE.search(stripped)


def _collect_python_todos(path: Path, project_path: Path, limit: int) -> list[str]:
    hits: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for token in tokenize.generate_tokens(fh.readline):
                if token.type != tokenize.COMMENT:
                    continue
                m = _TODO_RE.search(token.string)
                if not m:
                    continue
                rel = path.relative_to(project_path)
                hits.append(f"{rel}:{token.start[0]}  {m.group(0).strip()}")
                if len(hits) >= limit:
                    break
    except Exception:
        return []
    return hits


def collect_todos(project_path: Path, limit: int = 20) -> str:
    hits: list[str] = []
    for path in project_path.rglob("*"):
        if len(hits) >= limit:
            break
        if not path.is_file() or path.suffix not in _SCAN_EXTS:
            continue
        # skip common heavy dirs
        parts = {p.lower() for p in path.parts}
        if parts & {"node_modules", ".git", "dist", "build", ".venv", "venv", "__pycache__"}:
            continue
        if path.suffix == ".py":
            hits.extend(_collect_python_todos(path, project_path, limit - len(hits)))
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                in_markdown_fence = False
                for lineno, line in enumerate(fh, 1):
                    if path.suffix == ".md":
                        stripped = line.lstrip()
                        if stripped.startswith("```") or stripped.startswith("~~~"):
                            in_markdown_fence = not in_markdown_fence
                            continue
                        m = _todo_match_in_markdown(line, in_markdown_fence)
                    else:
                        m = _todo_match_in_code_comment(path, line)
                    if m:
                        rel = path.relative_to(project_path)
                        hits.append(f"{rel}:{lineno}  {m.group(0).strip()}")
                        if len(hits) >= limit:
                            break
        except Exception:
            continue
    return "\n".join(hits) if hits else "（无）"


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
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return "（ruff 无发现）"
        return "\n".join(lines[:limit])
    except Exception as exc:
        return f"（ruff 执行失败：{exc}）"


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
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        output = (result.stdout or "") + (result.stderr or "")
        lines = [ln for ln in output.splitlines() if ln.strip()]
        if not lines:
            return "（pytest collect 无输出）"
        # 重点关心收集错误 / 警告 / 统计行
        interesting = [
            ln for ln in lines
            if "error" in ln.lower() or "warning" in ln.lower() or "test" in ln.lower()
        ]
        picked = interesting[:limit] if interesting else lines[-limit:]
        return "\n".join(picked)
    except Exception as exc:
        return f"（pytest collect 失败：{exc}）"


def _skip_scan_path(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return bool(parts & {"node_modules", ".git", "dist", "build", ".venv", "venv", "__pycache__"})


def _manifest_lock_status(manifest: Path, locks: tuple[str, ...]) -> str:
    existing = [manifest.parent / lock for lock in locks if (manifest.parent / lock).exists()]
    if not existing:
        return "missing"
    newest_lock = max(existing, key=lambda item: item.stat().st_mtime)
    return "stale" if manifest.stat().st_mtime > newest_lock.stat().st_mtime else "ok"


def _count_unpinned_requirements(path: Path) -> int:
    count = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(("-", "--")):
            continue
        if not any(op in line for op in ("==", ">=", "<=", "~=", ">", "<")):
            count += 1
    return count


def collect_dependency_health(project_path: Path, limit: int = 20) -> str:
    """Collect cheap, offline dependency health signals without hitting registries."""
    findings: list[str] = []
    package_manifests = [p for p in project_path.rglob("package.json") if p.is_file() and not _skip_scan_path(p)]
    for package_json in package_manifests:
        if len(findings) >= limit:
            break
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        has_dependencies = bool(data.get("dependencies") or data.get("devDependencies") or data.get("optionalDependencies"))
        if not has_dependencies:
            continue
        rel = package_json.relative_to(project_path)
        status = _manifest_lock_status(package_json, ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"))
        if status == "missing":
            findings.append(f"{rel}: 发现依赖但缺少 lockfile")
        elif status == "stale":
            findings.append(f"{rel}: package.json 比 lockfile 更新，可能需要刷新依赖锁")

    for req in project_path.rglob("requirements*.txt"):
        if len(findings) >= limit:
            break
        if not req.is_file() or _skip_scan_path(req):
            continue
        unpinned = _count_unpinned_requirements(req)
        if unpinned:
            rel = req.relative_to(project_path)
            findings.append(f"{rel}: {unpinned} 个依赖未固定版本")

    return "\n".join(findings[:limit]) if findings else "（无明显依赖健康问题；未联网检查最新版本）"


def _existing_titles(project: str) -> str:
    rows = [
        t
        for t in db.list_tasks(project=project)
        if t["status"] in {"backlog", "in_progress"}
    ]
    if not rows:
        return "（无）"
    return "\n".join(f"- {t['title']}" for t in rows[:30])


def _dedup_key(title: str, goal: str) -> str:
    text = (title.strip() + "|" + goal.strip()).lower()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _call_llm(
    prompt: str,
    classifier_provider: str,
    classifier_model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    project_path: str,
    timeout: int,
    planner: str = "claude",
) -> dict:
    # 1) Prefer the configured classifier provider (API with key).
    if classifier_provider and classifier_provider in API_PROVIDERS:
        from dataclasses import replace

        provider = replace(API_PROVIDERS[classifier_provider])
        if classifier_model:
            provider.model = classifier_model
        if api_key:
            provider.api_key = api_key
        if base_url:
            provider.base_url = base_url
        if not provider.requires_api_key() or provider.resolve_api_key():
            raw = _run_api_provider(provider, prompt).strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if "\n" in raw:
                    raw = raw.split("\n", 1)[1]
                if raw.endswith("```"):
                    raw = raw[:-3]
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                raw = raw[start : end + 1]
            return json.loads(raw)

    # 2) Fall back to local CLI — default claude (faster for analysis/inspection).
    normalized = normalize_agent_name(planner) if planner else "claude"
    if normalized in {"claude", "claude-node", "claude-sonnet", "claude-opus", "claude-haiku"}:
        return _run_claude_schema_prompt(
            prompt,
            INSPECT_SCHEMA,
            planner=normalized,
            project_path=project_path,
            timeout=timeout,
        )
    return _run_codex_schema_prompt(
        prompt,
        INSPECT_SCHEMA,
        project_path=project_path,
        timeout=timeout,
    )


def run_inspection(
    project_info: dict,
    *,
    max_new_tasks: int = 3,
    signals: tuple[str, ...] = ("git_log", "failed_tasks", "todos"),
    auto_execute: bool = False,
    priority: str = "P3",
    agent: str = "codex",
    planner: str = "claude",
    dry_run: bool = False,
    timeout: int = 120,
) -> dict:
    project_name = project_info["name"]
    project_path = Path(project_info["path"])

    git_log = collect_git_log(project_path) if "git_log" in signals else "（跳过）"
    failed = collect_failed_tasks(project_name) if "failed_tasks" in signals else "（跳过）"
    todos = collect_todos(project_path) if "todos" in signals else "（跳过）"
    ruff_report = collect_ruff(project_path) if "ruff" in signals else "（跳过）"
    pytest_report = collect_pytest_collect(project_path) if "pytest" in signals else "（跳过）"
    deps_report = collect_dependency_health(project_path) if "deps" in signals else "（跳过）"

    existing = _existing_titles(project_name)
    prompt = INSPECT_PROMPT.format(
        max_tasks=max_new_tasks,
        existing_titles=existing,
        git_log=git_log,
        failed_tasks=failed,
        todos=todos,
        ruff=ruff_report,
        pytest=pytest_report,
        deps=deps_report,
    )

    cfg = load_project_config(project_path)
    classifier_cfg = getattr(cfg, "classifier", None)
    provider_key = classifier_cfg.provider if classifier_cfg else ""
    model = classifier_cfg.model if classifier_cfg else ""
    api_key = cfg.get_provider_api_key(provider_key) if provider_key else None
    provider_cfg = cfg.providers.get(provider_key) if provider_key else None
    base_url = provider_cfg.base_url if provider_cfg else None

    try:
        payload = _call_llm(
            prompt,
            classifier_provider=provider_key,
            classifier_model=model,
            api_key=api_key,
            base_url=base_url,
            project_path=str(project_path),
            timeout=timeout,
            planner=planner,
        )
    except Exception as exc:
        return {
            "project": project_name,
            "error": f"巡检 LLM 调用失败：{exc}",
            "created": [],
        }

    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []

    # Dedup
    existing_keys = db.existing_dedup_keys(project_name)
    created = []
    skipped = []
    for item in candidates[:max_new_tasks]:
        title = (item.get("title") or "").strip()
        goal = (item.get("goal") or "").strip()
        if not title or not goal:
            continue
        key = _dedup_key(title, goal)
        if key in existing_keys:
            skipped.append({"title": title, "reason": "duplicate"})
            continue
        content = _build_content(item)
        if dry_run:
            created.append({"title": title, "goal": goal, "priority": item.get("priority") or priority})
            continue
        task = db.create_task(
            project=project_name,
            title=title,
            content=content,
            agent=agent,
            priority=item.get("priority") or priority,
            project_path=str(project_path),
            source="inspector",
            dedup_key=key,
        )
        created.append(task)
        existing_keys.add(key)

    return {
        "project": project_name,
        "candidates_total": len(candidates),
        "created": created,
        "skipped": skipped,
        "auto_execute": auto_execute,
    }


def _build_content(item: dict) -> str:
    kind = item.get("kind") or "chore"
    rationale = (item.get("rationale") or "").strip()
    goal = (item.get("goal") or "").strip()
    return "\n".join(
        [
            f"# {item.get('title','').strip()}",
            "",
            f"> 由 `codepilot inspect` 自动建议（kind={kind}）",
            "",
            "## 任务目标",
            "",
            goal or "（待补充）",
            "",
            "## 动机",
            "",
            rationale or "（待补充）",
            "",
            "## 备注",
            "",
            "- 这是自动巡检产生的候选任务，执行前请人工确认方向。",
        ]
    )


def _print_result(result: dict, dry_run: bool) -> None:
    """Print a single inspection result to the terminal."""
    if result.get("error"):
        echo(f"[red]{result['error']}[/red]")
        return

    echo(
        f"[green]候选总数 {result['candidates_total']}，"
        f"新建 {len(result['created'])}，跳过 {len(result['skipped'])}[/green]"
    )
    for task in result["created"]:
        if dry_run:
            echo(f"  [dim][dry-run][/dim] {task['title']}  [{task.get('priority')}]")
        else:
            echo(f"  #{task['id']}  {task['title']}  [{task['priority']}]  source=inspector")
    for skipped in result["skipped"]:
        echo(f"  [dim]跳过: {skipped['title']} ({skipped['reason']})[/dim]")


@click.command("inspect")
@click.option("--project", "-p", callback=_resolve_project_strict, help="项目名称")
@click.option("--max", "max_new", type=int, default=None, help="本轮最多新增任务数")
@click.option("--dry-run", is_flag=True, help="只打印候选，不落库")
@click.option("--agent", default="codex", help="给新任务指定执行智能体（默认 codex，负责写代码）")
@click.option(
    "--planner",
    default=None,
    help="巡检用的 LLM；优先级：显式参数 > [inspect].planner > [agents].planner > codex",
)
@click.option("--json", "json_mode", is_flag=True, help="以 JSON 输出结果，便于脚本和其他 AI 调用")
@click.option("--interval", type=int, default=None, help="巡检间隔秒数（默认 1800）")
@click.option("--once", is_flag=True, help="仅巡检一次后退出")
@click.option("--foreground", is_flag=True, help="以前台持续巡检模式运行")
@click.option("--status", "show_status", is_flag=True, help="查看项目巡检进程状态")
@click.option("--stop", "stop_service", is_flag=True, help="停止项目巡检进程")
def inspect(
    project: str,
    max_new: Optional[int],
    dry_run: bool,
    agent: str,
    planner: Optional[str],
    json_mode: bool,
    interval: Optional[int],
    once: bool,
    foreground: bool,
    show_status: bool,
    stop_service: bool,
) -> None:
    """扫描项目信号，将可优化点作为候选任务产出.

    不带 --once 时将持续运行，每轮之间间隔 *interval* 秒（默认 1800 = 30 分钟）.
    传入 --once 则只巡检一轮后退出.
    """
    db.init_db()
    proj = db.get_project(project) if project else None
    if not proj:
        raise click.ClickException("需要用 -p 指定项目，或先 codepilot init")
    if show_status:
        status = inspect_service_status(project)
        if status["running"]:
            echo(f"[green]巡检运行中[/green]  PID={status['pid']}  项目={project}")
            echo(f"[dim]日志: {status['log']}[/dim]")
        else:
            echo(f"[dim]项目 {project} 巡检未运行[/dim]")
        return
    if stop_service:
        try:
            result = stop_inspect_service(project)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if result["stopped"]:
            echo(f"[green]项目 {project} 巡检已停止[/green]  PID={','.join(str(pid) for pid in result['pids'])}")
        else:
            echo(f"[dim]项目 {project} 巡检未运行[/dim]")
        return
    if not once and not foreground and not json_mode:
        try:
            result = start_inspect_service(
                project,
                max_new=max_new,
                dry_run=dry_run,
                agent=agent,
                planner=planner,
                interval=interval,
            )
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if result["started"]:
            echo(f"[green]项目 {project} 巡检已后台启动[/green]  PID={result['pid']}")
        else:
            echo(f"[yellow]项目 {project} 巡检已在运行[/yellow]  PID={result['pid']}")
        echo(f"[dim]日志: {result['log']}[/dim]")
        return
    project_info = {"name": proj["name"], "path": proj["path"]}

    cfg = load_project_config(Path(proj["path"]))
    ins = cfg.inspect
    limit = max_new if max_new is not None else ins.max_new_tasks_per_round
    sleep_seconds = interval if interval is not None else ins.interval_seconds
    effective_planner = resolve_planner(cfg, "inspect", explicit=planner)

    round_num = 0
    try:
        while True:
            db.touch_service_state(
                "inspect",
                project,
                pid=os.getpid(),
                log_path=str(_service_log_path(project)),
                status="running",
            )
            round_num += 1
            if not json_mode:
                head = f"巡检项目 {project_info['name']}"
                if not once:
                    head += f"  第 {round_num} 轮"
                echo(
                    f"[cyan]{head}[/cyan]  planner={effective_planner}  agent={agent}  "
                    f"max={limit}  signals={','.join(ins.signals)}"
                )

            result = run_inspection(
                project_info,
                max_new_tasks=limit,
                signals=ins.signals,
                auto_execute=ins.auto_execute,
                priority=ins.priority,
                agent=agent,
                planner=effective_planner,
                dry_run=dry_run,
            )

            if json_mode:
                click.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                _print_result(result, dry_run)

            if once:
                break

            if not json_mode:
                echo(f"[dim]下次巡检将在 {sleep_seconds} 秒后...[/dim]")
            try:
                time.sleep(sleep_seconds)
            except KeyboardInterrupt:
                if not json_mode:
                    echo("[yellow]巡检已停止[/yellow]")
                break
    finally:
        db.clear_service_state("inspect", project)
