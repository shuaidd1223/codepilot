"""Natural-language workflow entrypoints and interactive chat mode.

The planning/execution pipeline lives in :mod:`codepilot.commands.auto_workflow`
and the interactive REPL lives in :mod:`codepilot.commands.auto_chat`. This
module is the public shell: it re-exports the implementation APIs so historical
imports (``from codepilot.commands.auto import run_requirement_workflow``,
``from codepilot.commands.auto import run_chat_session``, etc.) keep working,
and it owns the click command definitions (``auto``, ``go``, ``chat``).

Functions from :mod:`codepilot.ai`, :mod:`codepilot.commands.status` and
:mod:`codepilot.commands.run` are also re-exported here so tests that
monkeypatch them via ``codepilot.commands.auto`` (e.g.
``monkeypatch.setattr(auto_cmd, "generate_task_breakdown", ...)``) continue to
drive the behavior observed inside the workflow and chat helpers.
"""

from __future__ import annotations

from typing import Optional

import click

# Re-exported dependencies — tests monkeypatch these on ``codepilot.commands.auto``
# and the implementation modules resolve them via this shell at call time.
from codepilot.ai import (  # noqa: F401 (re-export)
    answer_question_via_api,
    build_task_markdown_from_plan,
    check_provider_availability,
    classify_intent,
    generate_task_breakdown,
    normalize_agent_name,
    parse_automation_planner_result,
)
from codepilot.commands.add import _resolve_project_strict
from codepilot.commands.run import run_backlog  # noqa: F401 (re-export)
from codepilot.commands.status import (  # noqa: F401 (re-export)
    render_project_dashboard,
    render_project_stats,
)

from codepilot.commands.auto_chat import (  # noqa: F401 (re-export)
    _Spinner,
    _chat_help,
    _parse_intent_prefix,
    _start_chat_ui,
    run_chat_session,
)
from codepilot.commands.auto_workflow import (  # noqa: F401 (re-export)
    _project_config,
    _provider_context,
    _has_explicit_automation_task_agent,
    _resolve_effective_options,
    _resolve_task_agent,
    _should_execute,
    _should_fallback_codex_planning,
    clarify_requirement,
    resolve_project_for_prompt,
    run_requirement_workflow,
)


def _json_mode(ctx: click.Context, json_mode: bool) -> bool:
    if not json_mode and ctx.parent:
        json_mode = ctx.parent.obj.get("json_mode", False)
    return json_mode


def _root_options(ctx: click.Context) -> dict:
    root = ctx.find_root()
    return root.obj if root and root.obj else {}


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
    """将一个高层目标拆分为子任务，并可选立即执行."""
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
    """接收纯文本需求，判定复杂度后自动规划或直接执行."""
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
@click.option("--ui/--no-ui", "enable_ui", default=True, help="是否自动启动 Web UI（默认开启）")
@click.option("--ui-port", type=int, default=8766, help="Web UI 端口")
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
    enable_ui: bool,
    ui_port: int,
):
    """启动交互式自然语言会话."""
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
        enable_ui=enable_ui,
        ui_port=ui_port,
    )
