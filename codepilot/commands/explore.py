"""Read-only project exploration command."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.output import echo
from codepilot.storage import database as db


MAX_FILE_MATCHES = 20
MAX_FILE_LIST_ITEMS = 40
MAX_TASK_MATCHES = 10
MAX_SOURCE_ITEMS = 30
TEXT_SUFFIXES = {
    "",
    ".bat",
    ".cmd",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
    ".venv",
}
STOP_WORDS = {
    "about",
    "file",
    "files",
    "find",
    "for",
    "in",
    "list",
    "locate",
    "me",
    "please",
    "project",
    "search",
    "show",
    "the",
    "what",
    "where",
}
MUTATING_PATTERNS = (
    r"\b(delete|remove|rm|write|modify|change|update|fix|implement|create|add|install|start|stop|commit|push|merge|run|execute|test)\b",
    r"(删除|移除|写入|修改|更改|更新|修复|实现|新增|创建|安装|启动|停止|提交|推送|合并|执行|运行|测试)",
)
SHELL_META_PATTERN = re.compile(r"(\|\||&&|;|`|\$\(|>|<)")


def _shorten(text: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _extract_terms(query: str) -> list[str]:
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9_./-]{2,}|[\u4e00-\u9fff]{2,}", query or ""):
        term = raw.strip(" .,/\\'\"`").lower()
        if not term or term in STOP_WORDS:
            continue
        if term not in terms:
            terms.append(term)
    return terms[:8]


def _looks_unsafe_or_mutating(query: str) -> bool:
    text = (query or "").strip().lower()
    if not text:
        return False
    if SHELL_META_PATTERN.search(text):
        return True
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in MUTATING_PATTERNS)


def _resolve_project_info(project: str | None, cwd: Path | None = None) -> dict | None:
    db.init_db()
    if project:
        return db.get_project(project)
    found = db.find_project_by_path(cwd or Path.cwd())
    if found:
        return found
    projects = db.list_projects()
    if len(projects) == 1:
        return projects[0]
    return None


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _iter_candidate_files(root: Path):
    for current_raw, dirs, files in os.walk(root):
        dirs[:] = [item for item in dirs if item not in SKIP_DIRS]
        current = Path(current_raw)
        for name in files:
            path = current / name
            if path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def _fallback_search(root: Path, terms: list[str], *, limit: int = MAX_FILE_MATCHES) -> list[dict[str, Any]]:
    if not terms:
        return []
    matches: list[dict[str, Any]] = []
    lowered_terms = [term.lower() for term in terms]
    for path in _iter_candidate_files(root):
        rel = _safe_relative(path, root)
        path_hit = any(term in rel.lower() for term in lowered_terms)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if path_hit:
            matches.append({"path": rel, "line_no": 0, "line": "(path match)"})
        for number, line in enumerate(lines, start=1):
            haystack = line.lower()
            if any(term in haystack for term in lowered_terms):
                matches.append({"path": rel, "line_no": number, "line": _shorten(line, 180)})
                if len(matches) >= limit:
                    return matches
        if len(matches) >= limit:
            return matches
    return matches


def _file_list_evidence(root: Path, terms: list[str]) -> dict[str, Any]:
    lowered_terms = [term.lower() for term in terms]
    paths: list[str] = []
    for path in _iter_candidate_files(root):
        rel = _safe_relative(path, root)
        if not lowered_terms or any(term in rel.lower() for term in lowered_terms):
            paths.append(rel)
        if len(paths) >= MAX_FILE_LIST_ITEMS:
            break
    return {
        "kind": "file_list",
        "title": "文件列表",
        "summary": f"列出 {len(paths)} 个{'路径匹配' if lowered_terms else '项目'}文件。",
        "files": paths,
    }


def _rg_search(root: Path, terms: list[str], limitations: list[str], *, limit: int = MAX_FILE_MATCHES) -> list[dict[str, Any]]:
    rg = shutil.which("rg")
    if not rg:
        limitations.append("rg 不可用，已使用 Python 文件扫描 fallback。")
        return _fallback_search(root, terms, limit=limit)

    matches: list[dict[str, Any]] = []
    for term in terms:
        if len(matches) >= limit:
            break
        try:
            result = subprocess.run(
                [
                    rg,
                    "--line-number",
                    "--ignore-case",
                    "--max-count",
                    "5",
                    "--glob",
                    "!.git/*",
                    "--glob",
                    "!node_modules/*",
                    "--glob",
                    "!dist/*",
                    term,
                    str(root),
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            limitations.append(f"rg 调用失败，已使用 Python 文件扫描 fallback：{exc}")
            return _fallback_search(root, terms, limit=limit)
        if result.returncode not in {0, 1}:
            limitations.append(f"rg 返回 {result.returncode}，已使用 Python 文件扫描 fallback。")
            return _fallback_search(root, terms, limit=limit)
        for line in result.stdout.splitlines():
            parsed = _parse_rg_line(line, root)
            if parsed and parsed not in matches:
                matches.append(parsed)
            if len(matches) >= limit:
                break
    return matches


def _parse_rg_line(line: str, root: Path) -> dict[str, Any] | None:
    parts = line.rsplit(":", 2)
    if len(parts) != 3:
        return None
    raw_path, raw_number, content = parts
    try:
        line_no = int(raw_number)
    except ValueError:
        return None
    path = Path(raw_path)
    rel = _safe_relative(path, root) if path.is_absolute() else path.as_posix()
    return {"path": rel, "line_no": line_no, "line": _shorten(content, 180)}


def _run_readonly_command(args: list[str], root: Path, *, timeout: int = 8) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            args,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git_commit_refs(query: str) -> list[str]:
    refs: list[str] = []
    for match in re.findall(r"\b[0-9a-fA-F]{7,40}\b", query or ""):
        ref = match.lower()
        if ref not in refs:
            refs.append(ref)
    return refs[:3]


def _git_evidence(root: Path, query: str, limitations: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if not (root / ".git").exists():
        limitations.append("项目目录不是 Git 工作树或未包含 .git，跳过 Git evidence。")
        return evidence, sources

    status_code, status_out, status_err = _run_readonly_command(["git", "status", "--short"], root)
    log_code, log_out, log_err = _run_readonly_command(["git", "log", "--oneline", "-5"], root)
    if status_code == 0:
        evidence.append(
            {
                "kind": "git_status",
                "title": "Git 工作区状态",
                "summary": status_out or "工作区无未提交变更。",
            }
        )
        sources.append({"type": "git", "command": "git status --short"})
    elif status_err:
        limitations.append(f"git status 读取失败：{_shorten(status_err)}")
    if log_code == 0:
        evidence.append(
            {
                "kind": "git_log",
                "title": "最近提交",
                "summary": log_out or "未读取到提交记录。",
            }
        )
        sources.append({"type": "git", "command": "git log --oneline -5"})
    elif log_err:
        limitations.append(f"git log 读取失败：{_shorten(log_err)}")

    for ref in _git_commit_refs(query):
        show_code, show_out, show_err = _run_readonly_command(["git", "show", "--stat", "--oneline", "--no-renames", ref], root)
        if show_code == 0:
            evidence.append(
                {
                    "kind": "git_show",
                    "title": f"提交 {ref} 摘要",
                    "summary": show_out or "未读取到提交详情。",
                    "ref": ref,
                }
            )
            sources.append({"type": "git", "command": f"git show --stat --oneline --no-renames {ref}"})
        elif show_err:
            limitations.append(f"git show {ref} 读取失败：{_shorten(show_err)}")
    return evidence, sources


def _task_evidence(project_name: str, terms: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stats = db.get_task_stats(project_name)
    tasks = db.list_tasks(project=project_name)
    lowered_terms = [term.lower() for term in terms]
    matched_tasks: list[dict[str, Any]] = []
    for task in tasks:
        haystack = " ".join(
            str(task.get(key) or "")
            for key in ("id", "title", "content", "status", "priority", "agent", "error_message", "last_output")
        ).lower()
        if not lowered_terms or any(term in haystack for term in lowered_terms):
            matched_tasks.append(
                {
                    "id": task["id"],
                    "title": task.get("title"),
                    "status": task.get("status"),
                    "priority": task.get("priority"),
                    "agent": task.get("agent"),
                }
            )
        if len(matched_tasks) >= MAX_TASK_MATCHES:
            break

    evidence = [
        {
            "kind": "project_status",
            "title": "项目任务状态",
            "summary": (
                f"total={stats['total']} backlog={stats['backlog']} in_progress={stats['in_progress']} "
                f"failed={stats['failed']} cancelled={stats['cancelled']} done={stats['done']}"
            ),
            "stats": stats,
        }
    ]
    sources = [{"type": "database", "table": "tasks", "project": project_name}]
    if matched_tasks:
        evidence.append(
            {
                "kind": "task_search",
                "title": "任务匹配",
                "summary": f"匹配到 {len(matched_tasks)} 条任务。",
                "tasks": matched_tasks,
            }
        )
        for task in matched_tasks[:5]:
            logs = db.list_task_logs(int(task["id"]))
            if logs:
                evidence.append(
                    {
                        "kind": "task_log",
                        "title": f"任务 #{task['id']} 日志摘要",
                        "summary": _shorten((logs[-1].get("output") or ""), 300),
                        "task_id": task["id"],
                        "phase": logs[-1].get("phase"),
                        "exit_code": logs[-1].get("exit_code"),
                    }
                )
                sources.append({"type": "task_log", "task_id": task["id"]})
    return evidence, sources


def _inspect_signal_evidence(project_name: str, root: Path, limitations: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        from codepilot.commands.inspect import collect_inspection_signal_results

        results = collect_inspection_signal_results(
            project_name,
            root,
            signals=("git_log", "failed_tasks", "todos"),
        )
    except Exception as exc:
        limitations.append(f"inspect signal 读取失败：{exc}")
        return [], []

    evidence: list[dict[str, Any]] = []
    for result in results:
        content = (result.content or "").strip()
        if not result.enabled or not content or (content.startswith("（") and content.endswith("）")):
            continue
        evidence.append(
            {
                "kind": "inspect_signal",
                "key": result.key,
                "title": result.title,
                "summary": _shorten(content, 500),
            }
        )
    sources = [{"type": "inspect_signal", "signals": ["git_log", "failed_tasks", "todos"]}] if evidence else []
    return evidence, sources


def explore_project(
    query: str,
    *,
    project: str | None = None,
    cwd: Path | None = None,
    use_wiki: bool = True,
) -> dict[str, Any]:
    """Collect bounded read-only evidence for a project query."""
    normalized_query = (query or "").strip()
    terms = _extract_terms(normalized_query)
    limitations: list[str] = [
        "只读探索入口：不会写文件、改 Git、启动服务、安装依赖或执行测试。",
    ]

    project_info = _resolve_project_info(project, cwd=cwd)
    if not project_info:
        return {
            "query": normalized_query,
            "project": project or "",
            "status": "error",
            "rejected": False,
            "evidence": [],
            "sources": [],
            "limitations": limitations + ["未找到已注册项目；请传入 --project 或先运行 codepilot init。"],
        }

    project_name = str(project_info["name"])
    root = Path(project_info["path"]).resolve()
    project_payload = {"name": project_name, "path": str(root)}

    if _looks_unsafe_or_mutating(normalized_query):
        return {
            "query": normalized_query,
            "project": project_payload,
            "status": "rejected",
            "rejected": True,
            "evidence": [],
            "sources": [],
            "limitations": limitations
            + [
                "该问题看起来需要修改、执行或 shell 操作；请改用普通 workflow / go / run，并由执行器处理。",
            ],
        }

    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    file_list = _file_list_evidence(root, terms)
    evidence.append(file_list)
    sources.extend({"type": "file", "path": path} for path in file_list.get("files", [])[:MAX_SOURCE_ITEMS])

    if use_wiki:
        try:
            from codepilot.commands.wiki import wiki_context

            wiki = wiki_context(project_info, normalized_query, enabled=True, limit=5)
            if wiki.get("results"):
                evidence.append(
                    {
                        "kind": "wiki_context",
                        "title": "Wiki 上下文",
                        "summary": f"匹配到 {len(wiki['results'])} 条项目 wiki 记忆。",
                        "results": wiki["results"],
                    }
                )
                sources.extend({"type": "wiki", "path": item["path"]} for item in wiki["results"])
        except Exception as exc:
            limitations.append(f"wiki context 读取失败：{_shorten(str(exc))}")

    file_matches = _rg_search(root, terms, limitations)
    if file_matches:
        evidence.append(
            {
                "kind": "file_search",
                "title": "文件搜索",
                "summary": f"按关键词 {', '.join(terms) or '(空)'} 匹配到 {len(file_matches)} 条结果。",
                "matches": file_matches,
            }
        )
        sources.extend(
            {"type": "file", "path": match["path"]}
            for match in file_matches[:MAX_SOURCE_ITEMS]
        )
    else:
        limitations.append("未从文件内容中找到明显匹配；可尝试提供更具体的关键词或路径。")

    git_evidence, git_sources = _git_evidence(root, normalized_query, limitations)
    task_evidence, task_sources = _task_evidence(project_name, terms)
    inspect_evidence, inspect_sources = _inspect_signal_evidence(project_name, root, limitations)
    evidence.extend(git_evidence)
    evidence.extend(task_evidence)
    evidence.extend(inspect_evidence)
    sources.extend(git_sources)
    sources.extend(task_sources)
    sources.extend(inspect_sources)

    return {
        "query": normalized_query,
        "project": project_payload,
        "status": "ok",
        "rejected": False,
        "evidence": evidence,
        "sources": sources[:MAX_SOURCE_ITEMS],
        "limitations": limitations,
    }


def _print_human_result(result: dict[str, Any]) -> None:
    if result.get("status") == "error":
        raise click.ClickException((result.get("limitations") or ["探索失败"])[-1])
    if result.get("rejected"):
        echo("[yellow]已拒绝：explore 只支持只读探索。[/yellow]")
        for item in result.get("limitations") or []:
            echo(f"  - {item}")
        return
    project = result.get("project") or {}
    echo(f"[cyan]Explore[/cyan] {project.get('name', '')}  query={result.get('query')}")
    for item in result.get("evidence") or []:
        echo(f"\n[bold]{item.get('title') or item.get('kind')}[/bold]")
        echo(str(item.get("summary") or ""))
        for match in item.get("matches") or []:
            line_no = match.get("line_no") or 0
            suffix = f":{line_no}" if line_no else ""
            echo(f"  {match.get('path')}{suffix}  {match.get('line')}")
    if result.get("limitations"):
        echo("\n[dim]限制[/dim]")
        for item in result["limitations"]:
            echo(f"  - {item}")


@click.command("explore")
@click.argument("query_parts", nargs=-1)
@click.option("--prompt", "prompt", default=None, help="要探索的问题；等价于位置参数")
@click.option("--project", "-p", default=None, help="项目名称；不传则按当前目录自动识别")
@click.option("--use-wiki/--no-wiki", default=True, show_default=True, help="是否读取项目 wiki 作为只读上下文")
@click.option("--json", "json_mode", is_flag=True, help="以 JSON 输出结构化 evidence")
@click.pass_context
def explore(
    ctx: click.Context,
    query_parts: tuple[str, ...],
    prompt: str | None,
    project: str | None,
    use_wiki: bool,
    json_mode: bool,
) -> None:
    """只读探索项目文件、Git、任务和 inspect 信号."""
    json_mode = resolve_json_mode(ctx, json_mode)
    query = (prompt or " ".join(query_parts)).strip()
    if not query:
        raise click.ClickException("需要提供探索问题，例如 codepilot explore --prompt \"find task template\"")

    result = explore_project(query, project=project, use_wiki=use_wiki)
    if result.get("status") == "error":
        if json_mode:
            emit_json_payload("explore", ok=False, data=result, error=(result.get("limitations") or ["探索失败"])[-1], error_code="project_required")
            ctx.exit(1)
        _print_human_result(result)
        return

    if json_mode:
        emit_json_payload("explore", ok=True, data=result)
        return
    _print_human_result(result)
