"""Periodic codebase inspection: surface improvement candidates and enqueue them."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
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
from codepilot.config import load_project_config
from codepilot.output import echo

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


_TODO_RE = re.compile(r"(TODO|FIXME|XXX)[:：]?\s*(.{0,120})", re.IGNORECASE)
_SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".md"}


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
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    m = _TODO_RE.search(line)
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

    existing = _existing_titles(project_name)
    prompt = INSPECT_PROMPT.format(
        max_tasks=max_new_tasks,
        existing_titles=existing,
        git_log=git_log,
        failed_tasks=failed,
        todos=todos,
        ruff=ruff_report,
        pytest=pytest_report,
    )

    cfg = load_project_config(project_path)
    classifier_cfg = getattr(cfg, "classifier", None)
    provider_key = classifier_cfg.provider if classifier_cfg else ""
    model = classifier_cfg.model if classifier_cfg else ""
    api_key = cfg.get_provider_api_key(provider_key) if provider_key else None

    try:
        payload = _call_llm(
            prompt,
            classifier_provider=provider_key,
            classifier_model=model,
            api_key=api_key,
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
    help="巡检用的 LLM（默认 claude，从 [inspect].planner 读取；可选 claude / codex）",
)
@click.option("--json", "json_mode", is_flag=True, help="以 JSON 输出结果，便于脚本和其他 AI 调用")
@click.option("--interval", type=int, default=None, help="巡检间隔秒数（默认 1800）")
@click.option("--once", is_flag=True, help="仅巡检一次后退出")
def inspect(
    project: str,
    max_new: Optional[int],
    dry_run: bool,
    agent: str,
    planner: Optional[str],
    json_mode: bool,
    interval: Optional[int],
    once: bool,
) -> None:
    """扫描项目信号，将可优化点作为候选任务产出.

    不带 --once 时将持续运行，每轮之间间隔 *interval* 秒（默认 1800 = 30 分钟）.
    传入 --once 则只巡检一轮后退出.
    """
    db.init_db()
    proj = db.get_project(project) if project else None
    if not proj:
        raise click.ClickException("需要用 -p 指定项目，或先 codepilot init")
    project_info = {"name": proj["name"], "path": proj["path"]}

    cfg = load_project_config(Path(proj["path"]))
    ins = cfg.inspect
    limit = max_new if max_new is not None else ins.max_new_tasks_per_round
    sleep_seconds = interval if interval is not None else ins.interval_seconds
    effective_planner = planner or ins.planner or "claude"

    round_num = 0
    while True:
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
