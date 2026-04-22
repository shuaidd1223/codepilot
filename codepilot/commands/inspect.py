"""Periodic codebase inspection: surface improvement candidates and enqueue them."""

from __future__ import annotations

import hashlib
import json
import os
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
from codepilot.commands import inspect_service, inspect_signals
from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.config import load_project_config, resolve_planner
from codepilot.output import echo
from codepilot.paths import global_storage_root
from codepilot.runtime import is_process_alive, stop_process_tree

INSPECT_STATE_DIR = global_storage_root() / "inspect"


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _service_log_path(project: str) -> Path:
    return inspect_service.service_log_path(project, state_dir=INSPECT_STATE_DIR)


def inspect_service_status(project: str) -> dict:
    return inspect_service.inspect_service_status(
        project,
        state_dir=INSPECT_STATE_DIR,
        get_service_state=db.get_service_state,
        is_alive=is_process_alive,
    )


def _cleanup_inspect_files(project: str) -> None:
    inspect_service.cleanup_inspect_files(
        project,
        clear_service_state=db.clear_service_state,
    )


def _write_inspect_meta(project: str, pid: int, *, interval: int, planner: str, agent: str) -> None:
    inspect_service.write_inspect_meta(
        project,
        pid,
        interval=interval,
        planner=planner,
        agent=agent,
        state_dir=INSPECT_STATE_DIR,
        upsert_service_state=db.upsert_service_state,
        now_iso_fn=_now_iso,
    )


def _spawn_detached_inspect(
    project: str,
    *,
    max_new: int | None,
    dry_run: bool,
    agent: str,
    planner: str | None,
    interval: int | None,
) -> object:
    return inspect_service.spawn_detached_inspect(
        project,
        max_new=max_new,
        dry_run=dry_run,
        agent=agent,
        planner=planner,
        interval=interval,
        state_dir=INSPECT_STATE_DIR,
        now_iso_fn=_now_iso,
    )


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
    cfg = load_project_config(proj) if proj else None
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

## 信号 7：代码规模与复杂度线索
{code_metrics}

请以下列 JSON 返回：{{"candidates": [{{"title": "...", "goal": "...", "priority": "P3", "rationale": "...", "kind": "refactor"}}]}}
如果没有值得提的改进项，返回 {{"candidates": []}}。
"""


collect_git_log = inspect_signals.collect_git_log
collect_failed_tasks = inspect_signals.collect_failed_tasks
collect_todos = inspect_signals.collect_todos
collect_ruff = inspect_signals.collect_ruff
collect_pytest_collect = inspect_signals.collect_pytest_collect
collect_dependency_health = inspect_signals.collect_dependency_health
collect_code_metrics = inspect_signals.collect_code_metrics


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


def collect_inspection_signals(
    project_name: str,
    project_path: Path,
    *,
    signals: tuple[str, ...],
) -> dict[str, str]:
    """Collect all inspect signals and return a normalized signal map."""
    enabled = set(signals)
    signal_map = {
        "git_log": collect_git_log(project_path) if "git_log" in enabled else "（跳过）",
        "failed_tasks": collect_failed_tasks(project_name) if "failed_tasks" in enabled else "（跳过）",
        "todos": collect_todos(project_path) if "todos" in enabled else "（跳过）",
        "ruff": collect_ruff(project_path) if "ruff" in enabled else "（跳过）",
        "pytest": collect_pytest_collect(project_path) if "pytest" in enabled else "（跳过）",
        "deps": collect_dependency_health(project_path) if "deps" in enabled else "（跳过）",
    }
    code_metrics_enabled = bool({"code_metrics", "code_size", "complexity"} & enabled)
    signal_map["code_metrics"] = collect_code_metrics(project_path) if code_metrics_enabled else "（跳过）"
    return signal_map


def _build_inspection_prompt(
    *,
    project_name: str,
    max_new_tasks: int,
    signal_map: dict[str, str],
) -> str:
    existing = _existing_titles(project_name)
    return INSPECT_PROMPT.format(
        max_tasks=max_new_tasks,
        existing_titles=existing,
        git_log=signal_map["git_log"],
        failed_tasks=signal_map["failed_tasks"],
        todos=signal_map["todos"],
        ruff=signal_map["ruff"],
        pytest=signal_map["pytest"],
        deps=signal_map["deps"],
        code_metrics=signal_map["code_metrics"],
    )


def _extract_candidates(payload: dict) -> list[dict]:
    candidates = payload.get("candidates") or []
    return candidates if isinstance(candidates, list) else []


def _materialize_inspection_output(
    candidates: list[dict],
    *,
    max_new_tasks: int,
    project_name: str,
    project_path: Path,
    priority: str,
    agent: str,
    dry_run: bool,
) -> tuple[list[dict], list[dict]]:
    """Write candidate output to the selected path (preview or DB)."""
    existing_keys = db.existing_dedup_keys(project_name)
    created: list[dict] = []
    skipped: list[dict] = []
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
    return created, skipped


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

    signal_map = collect_inspection_signals(
        project_name,
        project_path,
        signals=signals,
    )
    prompt = _build_inspection_prompt(
        project_name=project_name,
        max_new_tasks=max_new_tasks,
        signal_map=signal_map,
    )

    cfg = load_project_config(project_info)
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

    candidates = _extract_candidates(payload)
    created, skipped = _materialize_inspection_output(
        candidates,
        max_new_tasks=max_new_tasks,
        project_name=project_name,
        project_path=project_path,
        priority=priority,
        agent=agent,
        dry_run=dry_run,
    )

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


def _print_round_header(*, project_name: str, planner: str, agent: str, max_new_tasks: int, signals: tuple[str, ...], once: bool, round_num: int) -> None:
    head = f"巡检项目 {project_name}"
    if not once:
        head += f"  第 {round_num} 轮"
    echo(
        f"[cyan]{head}[/cyan]  planner={planner}  agent={agent}  "
        f"max={max_new_tasks}  signals={','.join(signals)}"
    )


def _emit_inspection_result(result: dict, *, dry_run: bool, json_mode: bool) -> None:
    if json_mode:
        payload = dict(result)
        error = payload.pop("error", None)
        emit_json_payload(
            "inspect",
            ok=not bool(error),
            data=payload,
            error=str(error) if error else None,
            error_code="inspect_failed" if error else None,
        )
        return
    _print_result(result, dry_run)


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
@click.pass_context
def inspect(
    ctx: click.Context,
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
    json_mode = resolve_json_mode(ctx, json_mode)
    proj = db.get_project(project) if project else None
    if not proj:
        if json_mode:
            emit_json_payload(
                "inspect",
                ok=False,
                data={"project": project or "", "created": [], "skipped": [], "candidates_total": 0},
                error="需要用 -p 指定项目，或先 codepilot init",
                error_code="project_required",
            )
            ctx.exit(1)
            return
        raise click.ClickException("需要用 -p 指定项目，或先 codepilot init")
    if show_status:
        status = inspect_service_status(project)
        if json_mode:
            emit_json_payload("inspect", ok=True, data={"action": "status", "service": status})
        else:
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
            if json_mode:
                emit_json_payload(
                    "inspect",
                    ok=False,
                    data={"action": "stop", "service": inspect_service_status(project)},
                    error=str(exc),
                    error_code="stop_failed",
                )
                ctx.exit(1)
                return
            raise click.ClickException(str(exc)) from exc
        if json_mode:
            emit_json_payload(
                "inspect",
                ok=bool(result.get("stopped")),
                data={"action": "stop", "result": result},
                error=None if result.get("stopped") else f"项目 {project} 巡检未运行",
                error_code=None if result.get("stopped") else "service_not_running",
            )
        else:
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
    project_info = {"name": proj["name"], "path": proj["path"], "config_file": proj.get("config_file")}

    cfg = load_project_config(proj)
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
                _print_round_header(
                    project_name=project_info["name"],
                    planner=effective_planner,
                    agent=agent,
                    max_new_tasks=limit,
                    signals=ins.signals,
                    once=once,
                    round_num=round_num,
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

            _emit_inspection_result(result, dry_run=dry_run, json_mode=json_mode)

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

