"""Natural-language workflow entrypoints and interactive chat mode."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import click

from codepilot import db
from codepilot.ai import (
    answer_question_via_api,
    build_task_markdown_from_plan,
    check_provider_availability,
    classify_intent,
    generate_task_breakdown,
    normalize_agent_name,
)
from codepilot.commands.add import _resolve_project_strict
from codepilot.commands.run import run_backlog
from codepilot.commands.status import render_project_dashboard
from codepilot.config import find_config, load_config, load_project_config
from codepilot.output import echo, safe


def _json_mode(ctx: click.Context, json_mode: bool) -> bool:
    if not json_mode and ctx.parent:
        json_mode = ctx.parent.obj.get("json_mode", False)
    return json_mode


def _root_options(ctx: click.Context) -> dict:
    root = ctx.find_root()
    return root.obj if root and root.obj else {}


def _should_fallback_codex_planning(exc: Exception) -> bool:
    """Only fall back to a single Codex task on planner timeouts."""
    message = str(exc).lower()
    return "超时" in str(exc) or "timed out" in message or "timeout" in message


def resolve_project_for_prompt(project: Optional[str] = None, cwd: Optional[Path] = None) -> dict:
    """Resolve the target project, preferring the current working tree."""
    db.init_db()
    current_dir = Path(cwd or Path.cwd()).resolve()

    if project:
        proj = db.get_project(project)
        if not proj:
            raise click.ClickException(f"项目 '{project}' 未注册")
        return proj

    config_path = find_config(current_dir)
    if config_path:
        cfg = load_config(config_path)
        project_root = config_path.parent.resolve()
        matched = db.find_project_by_path(project_root)
        project_name = (cfg.project_name or cfg.project.name or project_root.name) if cfg else project_root.name
        if matched and Path(matched["path"]).resolve() == project_root:
            return db.register_project(
                name=matched["name"],
                path=str(project_root),
                base_branch=(cfg.base_branch if cfg else matched.get("base_branch", "dev")),
                default_mode=(cfg.default_mode if cfg else matched.get("default_mode", "codex")),
                worktree_base=(cfg.worktree_base if cfg else matched.get("worktree_base")),
                config_file=str(config_path),
            )

        return db.register_project(
            name=project_name,
            path=str(project_root),
            base_branch=(cfg.base_branch if cfg else "dev"),
            default_mode=(cfg.default_mode if cfg else "codex"),
            worktree_base=(cfg.worktree_base if cfg else None),
            config_file=str(config_path),
        )

    matched = db.find_project_by_path(current_dir)
    if matched:
        return matched

    if (current_dir / ".git").exists():
        return db.register_project(
            name=current_dir.name,
            path=str(current_dir),
            base_branch="main",
            default_mode="codex",
            config_file=None,
        )

    raise click.ClickException("未找到当前项目，请先运行 codepilot init，或在命令里显式指定 --project")


def _project_config(project_info: dict):
    return load_project_config(project_info.get("path"), config_file=project_info.get("config_file"))


def _provider_context(project_info: dict) -> str:
    """Prefer an explicitly stored AGENTS.toml path when resolving CLI providers."""
    return project_info.get("config_file") or project_info["path"]


def _should_execute(project_info: dict, execute: Optional[bool]) -> bool:
    if execute is not None:
        return execute

    cfg = _project_config(project_info)
    if cfg:
        if cfg.automation.confirm_before_execute and click.get_text_stream("stdin").isatty():
            return click.confirm("规划完成，是否立即开始执行？", default=cfg.automation.auto_execute)
        return cfg.automation.auto_execute
    return True


def _resolve_effective_options(
    project_info: dict,
    *,
    planner: Optional[str] = None,
    executor: Optional[str] = None,
    auto_commit: Optional[bool] = None,
    max_tasks: int = 0,
    max_retries: int = 0,
) -> dict:
    cfg = _project_config(project_info)
    return {
        "planner": planner or (cfg.automation.planner if cfg else "codex"),
        "executor": executor or (cfg.automation.executor if cfg else "builtin"),
        "auto_commit": (cfg.automation.auto_commit if auto_commit is None and cfg else (True if auto_commit is None else auto_commit)),
        "max_tasks": max_tasks or (cfg.automation.max_tasks if cfg else 5),
        "max_retries": max_retries or (cfg.automation.max_retries if cfg else 3),
        "confirm_before_execute": cfg.automation.confirm_before_execute if cfg else False,
        "auto_execute": cfg.automation.auto_execute if cfg else True,
    }


def _resolve_task_agent(project_info: dict, agent: Optional[str], executor: str) -> str:
    """Resolve task agent and fail early with a human-readable message when unavailable."""
    cfg = _project_config(project_info)
    default_agent = (
        cfg.project.default_mode
        if cfg and cfg.project and cfg.project.default_mode
        else project_info.get("default_mode") or "codex"
    )
    raw_agent = (agent or default_agent).strip()
    normalized = normalize_agent_name(raw_agent)

    provider_context = _provider_context(project_info)

    if normalized == "dual":
        if executor == "builtin":
            available, message = check_provider_availability("dual", project_path=provider_context)
            if not available:
                raise click.ClickException(message)
        return "dual"

    available, message = check_provider_availability(normalized, project_path=provider_context)
    if not available:
        raise click.ClickException(message)

    if executor == "builtin" and normalized not in {
        "codex",
        "claude",
        "claude-node",
        "claude-sonnet",
        "claude-opus",
        "claude-haiku",
    }:
        raise click.ClickException(
            f"内置执行器暂时不支持 `{raw_agent}`。请改用 codex、claude、claude-node 或 dual。"
        )

    return normalized


def run_requirement_workflow(
    *,
    project_info: dict,
    title: str,
    planner: str = "codex",
    task_agent: Optional[str] = None,
    priority: str = "P2",
    max_tasks: int = 5,
    execute: Optional[bool] = None,
    executor: str = "auto",
    auto_commit: bool = True,
    max_retries: int = 3,
    json_mode: bool = False,
) -> dict:
    """Plan one natural-language requirement and optionally execute it."""
    title = " ".join(title.strip().split())
    if not title:
        raise click.ClickException("需求文本不能为空")

    project_name = project_info["name"]
    project_path = project_info["path"]
    effective = _resolve_effective_options(
        project_info,
        planner=planner,
        executor=executor,
        auto_commit=auto_commit,
        max_tasks=max_tasks,
        max_retries=max_retries,
    )
    planner = effective["planner"]
    executor = effective["executor"]
    auto_commit = effective["auto_commit"]
    max_tasks = effective["max_tasks"]
    max_retries = effective["max_retries"]
    task_agent = _resolve_task_agent(project_info, task_agent, executor)

    echo(f"[cyan]收到需求：{title}[/cyan]")
    echo(f"[dim]  正在用 {planner} 规划任务，请稍候...[/dim]")
    try:
        breakdown = generate_task_breakdown(
            title=title,
            project_path=project_path,
            planner=planner,
            max_tasks=max_tasks,
            config_ref=_provider_context(project_info),
        )
    except Exception as exc:
        if normalize_agent_name(planner) == "codex" and _should_fallback_codex_planning(exc):
            echo("[yellow]Codex 规划没有及时完成，已降级为单任务直接执行。[/yellow]")
            breakdown = {
                "summary": "Codex 规划未完成，已降级为单任务执行",
                "complexity": "simple",
                "should_split": False,
                "tasks": [
                    {
                        "title": title,
                        "priority": priority,
                        "goal": title,
                        "acceptance_criteria": [
                            "完成当前需求的核心实现，主流程可以实际运行。",
                            "运行必要验证并在结果中说明是否通过。",
                        ],
                        "builder_notes": [
                            "先阅读相关代码，优先处理最影响可用性的阻塞点。",
                            "如果问题过大，先用最小可行改动把主链路跑通。",
                        ],
                        "reviewer_notes": [
                            "检查是否真的跑通主流程，而不只是修改文案或配置。",
                            "确认验证步骤和潜在回归风险已经说明。",
                        ],
                        "files": [],
                        "notes": [
                            f"本次为 Codex 规划失败后的降级执行。原始原因：{exc}",
                        ],
                    }
                ],
            }
        else:
            raise click.ClickException(str(exc)) from exc

    complexity = breakdown.get("complexity") or ("simple" if len(breakdown["tasks"]) <= 1 else "complex")
    should_split = breakdown.get("should_split")
    if should_split is None:
        should_split = len(breakdown["tasks"]) > 1

    created_tasks = []
    previous_task_id: int | None = None
    created_ids_by_index: list[int] = []
    for idx, item in enumerate(breakdown["tasks"]):
        # 优先使用 planner 给出的 depends_on_indices（支持 DAG 并行）；
        # 如果没给，则保持原来的线性依赖以保证行为兼容。
        dep_indices = item.get("depends_on_indices") or []
        dep_ids = [
            created_ids_by_index[i]
            for i in dep_indices
            if isinstance(i, int) and 0 <= i < len(created_ids_by_index)
        ]
        if not dep_ids and previous_task_id and not item.get("depends_on_indices"):
            dep_ids = [previous_task_id]
        task = db.create_task(
            project=project_name,
            title=item["title"],
            content=build_task_markdown_from_plan(item),
            agent=task_agent,
            priority=item.get("priority") or priority,
            depends_on=dep_ids or None,
            project_path=project_path,
            max_retries=max_retries,
        )
        created_tasks.append(task)
        created_ids_by_index.append(task["id"])
        previous_task_id = task["id"]

    will_execute = _should_execute(project_info, execute)

    payload = {
        "project": project_name,
        "summary": breakdown.get("summary", ""),
        "complexity": complexity,
        "should_split": should_split,
        "task_agent": task_agent,
        "tasks": created_tasks,
        "will_execute": will_execute,
    }

    if json_mode:
        if will_execute:
            payload["run"] = run_backlog(
                project_name,
                once=False,
                limit=len(created_tasks),
                executor=executor,
                auto_commit=auto_commit,
            )
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    label = "复杂任务" if should_split else "简单任务"
    echo(f"[green][OK] 已识别为{label}[/green]  complexity={complexity}")
    if breakdown.get("summary"):
        click.echo(f"  摘要: {breakdown['summary']}")
    for task in created_tasks:
        dep = f" depends_on=#{json.loads(task['depends_on'])[0]}" if task.get("depends_on") else ""
        click.echo(f"  #{task['id']}  {task['title']}  [{task['priority']}]  agent={task_agent}{dep}")
    render_project_dashboard(project_name, include_done=False, max_rows=max(8, len(created_tasks)), title="任务面板")

    if not will_execute:
        echo()
        echo("[dim]已完成规划，未自动执行[/dim]")
        return payload

    echo()
    echo("[cyan]开始自动执行...[/cyan]")
    stats = run_backlog(
        project_name,
        once=False,
        limit=len(created_tasks),
        executor=executor,
        auto_commit=auto_commit,
        retry_on_failure=False,
    )
    payload["run"] = stats
    echo(
        f"\n[dim]Workflow 完成: processed={stats['processed']} done={stats['done']} "
        f"failed={stats['failed']} requeued={stats['requeued']}[/dim]"
    )
    return payload


def _chat_help() -> str:
    return "\n".join(
        [
            "会话命令：",
            "  /help               查看帮助",
            "  /exit               退出会话",
            "  /status             查看当前项目任务看板",
            "  /history            查看对话记录",
            "  /clear              清空对话历史",
            "  /project <name>     切换项目",
            "  /agent <name>       切换默认任务智能体（如 codex / claude / dual）",
            "  /execute on|off     切换默认是否自动执行",
            "  /plan               只规划下一条需求，不执行",
            "  /run                自动执行下一条需求",
            "",
            "输入前缀（跳过自动分类）：",
            "  ? <文本>            当作问题直接回答，不建任务",
            "  ! <文本>            当作单任务，不拆分",
            "  # <文本>            当作需求，强制拆分",
            "",
            "会话内会自动记住上下文，连续提问无需重复说明。",
        ]
    )


def _parse_intent_prefix(text: str) -> tuple[Optional[str], str]:
    """Return (forced_intent, stripped_text). forced_intent ∈ {question,task,requirement} 或 None."""
    if not text:
        return None, text
    first, rest = text[0], text[1:].lstrip()
    if first == "?" and rest:
        return "question", rest
    if first == "!" and rest:
        return "task", rest
    if first == "#" and rest:
        return "requirement", rest
    return None, text


class _Spinner:
    """Simple inline spinner for long-running operations."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "思考中"):
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stderr.write(f"\r  {frame} {self._message}...")
            sys.stderr.flush()
            i += 1
            self._stop.wait(0.1)


def run_chat_session(
    *,
    project: Optional[str] = None,
    planner: Optional[str] = None,
    task_agent: Optional[str] = None,
    execute: Optional[bool] = None,
    executor: Optional[str] = None,
    auto_commit: Optional[bool] = None,
    max_tasks: int = 0,
    max_retries: int = 0,
) -> None:
    """Run a simple REPL that accepts plain-text requirements."""
    project_info = resolve_project_for_prompt(project)
    effective = _resolve_effective_options(
        project_info,
        planner=planner,
        executor=executor,
        auto_commit=auto_commit,
        max_tasks=max_tasks,
        max_retries=max_retries,
    )
    default_execute = effective["auto_execute"] if execute is None else execute
    default_agent = _resolve_task_agent(project_info, task_agent, effective["executor"])

    # 会话历史（跨 turn 记忆）
    chat_history: list[dict] = []

    echo(
        f"[cyan]CodePilot Chat[/cyan]  项目: {project_info['name']}  "
        f"planner={effective['planner']} executor={effective['executor']} agent={default_agent}"
    )
    echo("[dim]直接输入文本即可。问题会直接回答，需求会自动规划执行。[/dim]")
    echo("[dim]输入 /help 查看命令，/history 查看对话记录。[/dim]")
    echo()

    while True:
        try:
            raw = click.prompt("codepilot", prompt_suffix="> ", default="", show_default=False)
        except (EOFError, KeyboardInterrupt):
            echo()
            echo("[dim]会话已结束[/dim]")
            return

        text = raw.strip()
        if not text:
            continue

        if text.startswith("/"):
            parts = text.split()
            cmd = parts[0].lower()

            if cmd == "/exit":
                echo("[dim]会话已结束[/dim]")
                return
            if cmd == "/help":
                click.echo(_chat_help())
                continue
            if cmd == "/status":
                render_project_dashboard(
                    project_info["name"],
                    verbose=True,
                    include_done=False,
                    title="当前任务面板",
                )
                continue
            if cmd == "/project":
                if len(parts) < 2:
                    echo("[yellow]用法: /project <name>[/yellow]")
                    continue
                project_info = resolve_project_for_prompt(parts[1])
                effective = _resolve_effective_options(
                    project_info,
                    planner=planner,
                    executor=executor,
                    auto_commit=auto_commit,
                    max_tasks=max_tasks,
                    max_retries=max_retries,
                )
                default_agent = _resolve_task_agent(project_info, default_agent, effective["executor"])
                echo(f"[green][OK] 已切换项目[/green] {project_info['name']}")
                continue
            if cmd == "/agent":
                if len(parts) < 2:
                    echo("[yellow]用法: /agent <name>[/yellow]")
                    continue
                try:
                    default_agent = _resolve_task_agent(project_info, parts[1], effective["executor"])
                except click.ClickException as exc:
                    echo(f"[red]{exc.format_message()}[/red]")
                    continue
                echo(f"[green][OK] 默认任务智能体已设置为 {default_agent}[/green]")
                continue
            if cmd == "/execute":
                if len(parts) < 2 or parts[1].lower() not in {"on", "off"}:
                    echo("[yellow]用法: /execute on|off[/yellow]")
                    continue
                default_execute = parts[1].lower() == "on"
                echo(f"[green][OK] 自动执行已设置为 {default_execute}[/green]")
                continue
            if cmd == "/plan":
                default_execute = False
                echo("[green][OK] 下一条需求将只规划不执行[/green]")
                continue
            if cmd == "/run":
                default_execute = True
                echo("[green][OK] 下一条需求将自动执行[/green]")
                continue

            if cmd == "/history":
                if not chat_history:
                    echo("[dim]暂无对话记录[/dim]")
                else:
                    for i, turn in enumerate(chat_history, 1):
                        intent_tag = turn.get("intent", "?")
                        echo(f"[dim]#{i}[/dim] [{intent_tag}] {turn['user'][:80]}")
                        if turn.get("assistant"):
                            click.echo(f"  → {turn['assistant'][:120]}")
                continue
            if cmd == "/clear":
                chat_history.clear()
                echo("[green]对话历史已清空[/green]")
                continue

            echo("[yellow]未知会话命令[/yellow]")
            click.echo(_chat_help())
            continue

        forced_intent, payload_text = _parse_intent_prefix(text)
        spinner = _Spinner("处理中")
        spinner.__enter__()

        intent = forced_intent
        if intent is None:
            cfg = _project_config(project_info)
            classifier_cfg = getattr(cfg, "classifier", None)
            api_key = None
            classifier_provider = ""
            classifier_model = ""
            classifier_timeout = 30
            if classifier_cfg and classifier_cfg.enabled:
                classifier_provider = classifier_cfg.provider or ""
                classifier_model = classifier_cfg.model or ""
                classifier_timeout = classifier_cfg.timeout or 30
                if classifier_provider:
                    api_key = cfg.get_provider_api_key(classifier_provider)
            try:
                result = classify_intent(
                    payload_text,
                    project_path=project_info["path"],
                    classifier_provider=classifier_provider,
                    classifier_model=classifier_model,
                    timeout=classifier_timeout,
                    api_key=api_key,
                )
                intent = result["intent"]
            except Exception:
                intent = "requirement"

        # 分类完成后更新 spinner 文案
        intent_labels = {"question": "正在思考", "task": "正在执行", "requirement": "正在规划", "command": "处理中"}
        spinner._message = intent_labels.get(intent, "处理中")

        assistant_response = ""
        try:
            if intent == "command":
                spinner.__exit__(None, None, None)
                assistant_response = "请使用对应的 CLI 命令操作"
                echo(
                    "[yellow]这看起来是在调用 codepilot 自身命令，请直接用下面的入口：[/yellow]"
                )
                click.echo(
                    "  状态总览:  codepilot status -p <项目> -v\n"
                    "  任务日志:  codepilot logs <task_id>\n"
                    "  重试任务:  codepilot retry <task_id>\n"
                    "  停止任务:  codepilot stop <task_id>\n"
                    "  触发巡检:  codepilot inspect -p <项目>\n"
                    "  发布打包:  codepilot release prepare --version <版本>"
                )
                click.echo()
            elif intent == "question":
                cfg = _project_config(project_info)
                classifier_cfg = getattr(cfg, "classifier", None)
                provider_key = classifier_cfg.provider if classifier_cfg else ""
                api_key = cfg.get_provider_api_key(provider_key) if provider_key else None
                answer = answer_question_via_api(
                    provider_key=provider_key,
                    question=payload_text,
                    project_path=project_info["path"],
                    model_override=classifier_cfg.model if classifier_cfg else "",
                    api_key=api_key,
                    history=chat_history,
                )
                spinner.__exit__(None, None, None)
                if answer:
                    click.echo(answer)
                    assistant_response = answer
                else:
                    echo("[yellow]未获得回答[/yellow]")
            elif intent == "task":
                spinner.__exit__(None, None, None)
                run_requirement_workflow(
                    project_info=project_info,
                    title=payload_text,
                    planner=effective["planner"],
                    task_agent=default_agent,
                    execute=default_execute,
                    executor=effective["executor"],
                    auto_commit=effective["auto_commit"],
                    max_tasks=1,
                    max_retries=effective["max_retries"],
                )
                assistant_response = "任务已创建并执行"
            else:
                spinner.__exit__(None, None, None)
                run_requirement_workflow(
                    project_info=project_info,
                    title=payload_text,
                    planner=effective["planner"],
                    task_agent=default_agent,
                    execute=default_execute,
                    executor=effective["executor"],
                    auto_commit=effective["auto_commit"],
                    max_tasks=effective["max_tasks"],
                    max_retries=effective["max_retries"],
                )
                assistant_response = "需求已规划"
        except click.ClickException as exc:
            spinner.__exit__(None, None, None)
            echo(f"[red]{safe(exc.format_message())}[/red]")
            assistant_response = f"错误: {exc.format_message()}"
        except Exception as exc:
            spinner.__exit__(None, None, None)
            echo(f"[red]{safe(exc)}[/red]")
            assistant_response = f"错误: {exc}"

        # 保存对话历史
        chat_history.append({
            "user": payload_text,
            "assistant": assistant_response,
            "intent": intent or "unknown",
        })
        click.echo()


@click.command("auto")
@click.option("--project", "-p", callback=_resolve_project_strict, help="项目名称")
@click.option("--title", "-t", required=True, help="高层目标或任务标题")
@click.option("--planner", default="codex", help="用于拆分任务的规划器，默认 codex")
@click.option("--agent", "task_agent", default=None, help="创建出来的任务默认使用哪个智能体，如 codex / claude / dual")
@click.option("--priority", type=click.Choice(["P0", "P1", "P2", "P3"]), default="P2", help="默认优先级")
@click.option("--max-tasks", type=int, default=5, help="最多拆分出的子任务数量")
@click.option("--plan-only", is_flag=True, help="只拆分入队，不自动执行")
@click.option(
    "--executor",
    type=click.Choice(["auto", "dispatch", "builtin"], case_sensitive=False),
    default="auto",
    help="执行器类型",
)
@click.option("--auto-commit/--no-auto-commit", default=True, help="内置执行器成功后自动提交每个子任务")
@click.option("--max-retries", type=int, default=3, help="每个子任务的最大重试次数")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def auto(
    ctx: click.Context,
    project: str,
    title: str,
    planner: str,
    task_agent: Optional[str],
    priority: str,
    max_tasks: int,
    plan_only: bool,
    executor: str,
    auto_commit: bool,
    max_retries: int,
    json_mode: bool,
):
    """Split one high-level goal and optionally execute it."""
    json_mode = _json_mode(ctx, json_mode)
    project_info = resolve_project_for_prompt(project)
    try:
        run_requirement_workflow(
            project_info=project_info,
            title=title,
            planner=planner,
            task_agent=task_agent,
            priority=priority,
            max_tasks=max_tasks,
            execute=False if plan_only else True,
            executor=executor,
            auto_commit=auto_commit,
            max_retries=max_retries,
            json_mode=json_mode,
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@click.command("go")
@click.argument("requirement", nargs=-1, required=False)
@click.option("--project", help="项目名称；不指定则自动识别当前项目")
@click.option("--planner", default=None, help="规划器；默认读取配置或使用 codex")
@click.option("--agent", "task_agent", default=None, help="创建出来的任务默认使用哪个智能体，如 codex / claude / dual")
@click.option("--priority", type=click.Choice(["P0", "P1", "P2", "P3"]), default="P2", help="默认优先级")
@click.option("--max-tasks", type=int, default=0, help="最大拆分任务数；0 表示读取配置")
@click.option("--execute/--no-execute", default=None, help="是否立即执行；默认跟随配置")
@click.option(
    "--executor",
    type=click.Choice(["auto", "dispatch", "builtin"], case_sensitive=False),
    default=None,
    help="执行器类型；默认跟随配置",
)
@click.option("--auto-commit/--no-auto-commit", default=None, help="是否自动提交；默认跟随配置")
@click.option("--max-retries", type=int, default=0, help="最大重试次数；0 表示读取配置")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def go(
    ctx: click.Context,
    requirement: tuple[str, ...],
    project: Optional[str],
    planner: Optional[str],
    task_agent: Optional[str],
    priority: str,
    max_tasks: int,
    execute: Optional[bool],
    executor: Optional[str],
    auto_commit: Optional[bool],
    max_retries: int,
    json_mode: bool,
):
    """Accept plain text, decide complexity, then plan and optionally execute."""
    json_mode = _json_mode(ctx, json_mode)
    text = " ".join(requirement).strip()
    if not text:
        text = click.prompt("请输入你的需求")

    root_obj = _root_options(ctx)
    project = project or root_obj.get("direct_project")
    planner = planner or root_obj.get("planner")
    task_agent = task_agent or root_obj.get("agent")
    if execute is None:
        execute = root_obj.get("execute")
    executor = executor or root_obj.get("executor")
    if auto_commit is None:
        auto_commit = root_obj.get("auto_commit")
    if not max_tasks:
        max_tasks = root_obj.get("max_tasks", 0)
    if not max_retries:
        max_retries = root_obj.get("max_retries", 0)

    project_info = resolve_project_for_prompt(project)
    try:
        run_requirement_workflow(
            project_info=project_info,
            title=text,
            planner=planner or "codex",
            task_agent=task_agent,
            priority=priority,
            max_tasks=max_tasks or 5,
            execute=execute,
            executor=executor or "auto",
            auto_commit=auto_commit if auto_commit is not None else True,
            max_retries=max_retries or 3,
            json_mode=json_mode,
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@click.command("chat")
@click.option("--project", help="项目名称；不指定则自动识别当前项目")
@click.option("--planner", default=None, help="规划器；默认读取配置或使用 codex")
@click.option("--agent", "task_agent", default=None, help="默认创建任务时使用哪个智能体，如 codex / claude / dual")
@click.option("--execute/--no-execute", default=None, help="默认是否自动执行；默认跟随配置")
@click.option(
    "--executor",
    type=click.Choice(["auto", "dispatch", "builtin"], case_sensitive=False),
    default=None,
    help="执行器类型；默认跟随配置",
)
@click.option("--auto-commit/--no-auto-commit", default=None, help="是否自动提交；默认跟随配置")
@click.option("--max-tasks", type=int, default=0, help="最大拆分任务数；0 表示读取配置")
@click.option("--max-retries", type=int, default=0, help="最大重试次数；0 表示读取配置")
@click.pass_context
def chat(
    ctx: click.Context,
    project: Optional[str],
    planner: Optional[str],
    task_agent: Optional[str],
    execute: Optional[bool],
    executor: Optional[str],
    auto_commit: Optional[bool],
    max_tasks: int,
    max_retries: int,
):
    """Start an interactive natural-language session."""
    root_obj = _root_options(ctx)
    project = project or root_obj.get("direct_project")
    planner = planner or root_obj.get("planner")
    task_agent = task_agent or root_obj.get("agent")
    if execute is None:
        execute = root_obj.get("execute")
    executor = executor or root_obj.get("executor")
    if auto_commit is None:
        auto_commit = root_obj.get("auto_commit")
    if not max_tasks:
        max_tasks = root_obj.get("max_tasks", 0)
    if not max_retries:
        max_retries = root_obj.get("max_retries", 0)

    run_chat_session(
        project=project,
        planner=planner,
        task_agent=task_agent,
        execute=execute,
        executor=executor,
        auto_commit=auto_commit,
        max_tasks=max_tasks,
        max_retries=max_retries,
    )
