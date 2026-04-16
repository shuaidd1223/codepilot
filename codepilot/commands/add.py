"""codepilot add 命令：添加任务到 backlog（单条或批量）.

支持多种 AI Provider：CLI (claude/codex) 和 API (GPT-4/Claude/混元等)
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from codepilot import db
from codepilot.ai import (
    check_provider_availability,
    generate_task_content,
    list_available_providers,
    normalize_agent_name,
    resolve_agent_with_fallback,
)
from codepilot.commands.status import _resolve_project
from codepilot.output import echo


# ═══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_project_strict(ctx, param, value):
    """必须指定项目."""
    if not value:
        raise click.BadParameter("需要 --project 参数")
    db.init_db()
    proj = db.get_project(value)
    if not proj:
        echo(f"[red]错误: 项目 '{value}' 未注册[/red]")
        raise click.Abort()
    return value


def _resolve_agent_info(ctx, value) -> tuple[str, str | None, str | None]:
    """Resolve agent with fallback.  Returns (agent, fallback_reason, project_path)."""
    if not value:
        value = "codex"

    project_path = None
    default_mode = "codex"
    if ctx is not None and getattr(ctx, "params", None):
        project_name = ctx.params.get("project")
        project_info = db.get_project(project_name) if project_name else None
        if project_info:
            project_path = project_info.get("path")
            default_mode = project_info.get("default_mode") or "codex"

    agent, fallback_reason = resolve_agent_with_fallback(
        value, project_path=project_path, default_mode=default_mode,
    )

    # If fallback also failed, raise a clear error.
    if fallback_reason is None:
        available, msg = check_provider_availability(agent, project_path=project_path)
        if not available:
            providers = list_available_providers()
            hint_lines = []
            for cli in providers.get("cli", [])[:4]:
                hint_lines.append(f"CLI: {cli}")
            for api in providers.get("api", [])[:6]:
                hint_lines.append(f"API: {api}")
            hint = "；可选示例：" + "；".join(hint_lines) if hint_lines else ""
            raise click.BadParameter(msg + hint)

    return agent, fallback_reason, project_path


def _resolve_agent(ctx, param, value):
    """Click callback — resolve agent (backward-compatible signature)."""
    agent, _fallback_reason, _project_path = _resolve_agent_info(ctx, value)
    return agent


def _parse_batch_file(file_path: Path) -> list[dict]:
    """
    解析批量导入文件，支持两种格式：

    1. 每行一个标题（空行和 # 开头的行跳过）：
       优化日志输出
       新增用户认证
       修复登录 bug

    2. JSON 文件（.json 扩展名）：
       [{"title": "...", "priority": "P1", "agent": "claude-sonnet"}, ...]
    """
    content = file_path.read_text(encoding="utf-8").strip()
    if file_path.suffix.lower() == ".json":
        items = json.loads(content)
        return items

    # 纯文本格式：每行一个标题
    tasks = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tasks.append({"title": line})
    return tasks


# ═══════════════════════════════════════════════════════════════════════════════
# CLI 命令
# ═══════════════════════════════════════════════════════════════════════════════

# 支持的 Agent 选项
AGENT_CHOICES = [
    # CLI
    "claude", "claude-node", "codex", "gemini", "cloud",
    # API - OpenAI
    "openai-gpt4", "openai-gpt4o", "openai-gpt35",
    # API - Anthropic
    "claude-opus", "claude-sonnet", "claude-haiku",
    # API - 国内大模型
    "hunyuan", "zhipu-glm4", "wenxin", "qwen",
    # API - 其他
    "deepseek", "ollama", "groq",
    # 别名
    "gpt4", "gpt4o", "gpt35", "sonnet", "opus", "haiku",
    # 特殊模式
    "dual",
]


@click.command(context_settings={"allow_interspersed_args": False})
@click.option(
    "--project", "-p",
    callback=_resolve_project_strict,
    help="项目名称",
)
@click.option("--title", "-t", help="任务标题（单条模式）")
@click.option(
    "--agent", "-a",
    default="codex",
    callback=_resolve_agent,
    help="AI Provider (CLI: claude/codex | API: openai-gpt4/claude-sonnet/hunyuan/deepseek 等)"
)
@click.option("--priority", type=click.Choice(["P0", "P1", "P2", "P3"]), default="P2",
              help="优先级")
@click.option("--no-ai", is_flag=True, default=False, help="跳过 AI 生成，使用空白内容")
@click.option("--depends", "depends_on", default="", help="依赖的任务 ID，多个用逗号分隔")
@click.option("--file", "-f", "batch_file", type=click.Path(exists=True, path_type=Path),
              help="从文件批量导入任务（每行一个标题，或 .json 格式）")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def add(
    ctx: click.Context,
    project: str,
    title: str | None,
    agent: str,
    priority: str,
    no_ai: bool,
    depends_on: str,
    batch_file: Path | None,
    json_mode: bool,
):
    """
    添加任务到 backlog（单条或批量），支持 AI 自动生成任务内容。

    支持的 AI Provider：

      CLI 模式:
        claude       - Claude Code (官方 CLI)
        claude-node  - Claude Code (via Node.js)
        codex        - OpenAI Codex CLI
        gemini       - Google Gemini CLI

      API 模式 (需设置环境变量):
        openai-gpt4     - OpenAI GPT-4 Turbo
        openai-gpt4o    - OpenAI GPT-4o
        openai-gpt35    - OpenAI GPT-3.5 Turbo
        claude-opus     - Claude 3 Opus
        claude-sonnet   - Claude 3.5 Sonnet
        claude-haiku    - Claude 3 Haiku
        hunyuan         - 腾讯云混元
        zhipu-glm4      - 智谱 GLM-4
        wenxin          - 百度文心一言
        qwen            - 阿里通义千问
        deepseek        - DeepSeek
        ollama          - Ollama (本地)
        groq            - Groq

    示例:
      codepilot add -p myproj -t "优化日志输出"
      codepilot add -p myproj -t "新功能" -a openai-gpt4o
      codepilot add -p myproj -t "代码审查" -a claude-sonnet --priority P1
      codepilot add -p myproj -f tasks.txt
      codepilot add -p myproj -f tasks.json
    """
    db.init_db()
    # 优先用本地 --json，否则用全局
    if not json_mode and ctx.parent:
        json_mode = ctx.parent.obj.get("json_mode", False)
    proj_info = db.get_project(project)
    proj_path = proj_info.get("path", "") if proj_info else ""

    # 解析依赖
    dep_list: list[int] | None = None
    if depends_on:
        raw_ids = [d.strip() for d in depends_on.split(",") if d.strip()]
        dep_list = [int(d) for d in raw_ids if d.isdigit()]

    # 批量模式
    if batch_file:
        return _batch_add(
            project, batch_file, agent, priority, no_ai, dep_list,
            proj_path, json_mode,
        )

    # 单条模式
    if not title:
        raise click.BadParameter("--title 或 --file 必须指定一个")

    # Re-resolve with fallback info (the Click callback doesn't propagate it).
    default_mode = proj_info.get("default_mode", "codex") if proj_info else "codex"
    effective_agent, fallback_reason, _ = _resolve_agent_info(ctx, agent)

    _single_add(
        project, title, effective_agent, priority, no_ai, dep_list, proj_path,
        json_mode, fallback_reason=fallback_reason,
    )


def _single_add(
    project: str,
    title: str,
    agent: str,
    priority: str,
    no_ai: bool,
    dep_list: list[int] | None,
    proj_path: str,
    json_mode: bool,
    fallback_reason: str | None = None,
):
    """添加单个任务."""
    if fallback_reason:
        echo(f"[yellow]⚠ {fallback_reason}[/yellow]")

    if no_ai:
        content = ""
        echo("[yellow]跳过 AI 生成，内容为空[/yellow]")
    else:
        echo(f"[cyan]调用 {agent} 生成任务内容...[/cyan]")
        content = generate_task_content(title, project_path=proj_path, agent=agent)
        echo("[green][OK] AI 生成完成[/green]")

    task = db.create_task(
        project=project,
        title=title,
        content=content,
        agent=agent,
        priority=priority,
        depends_on=dep_list,
        fallback_reason=fallback_reason,
    )

    task_id = task["id"]

    if json_mode:
        click.echo(json.dumps(task, ensure_ascii=False, indent=2))
        return

    echo(f"[green]+ 任务 #{task_id} 已创建[/green]")
    click.echo(f"  项目:     {project}")
    click.echo(f"  标题:     {title}")
    click.echo(f"  Agent:    {agent}")
    if fallback_reason:
        click.echo(f"  回退原因: {fallback_reason}")
    click.echo(f"  优先级:   {priority}")
    if dep_list:
        click.echo(f"  依赖:     #{', #'.join(str(d) for d in dep_list)}")
    if content:
        preview = content[:150].replace("\n", " ")
        if len(content) > 150:
            preview += "..."
        click.echo(f"\n  内容预览: {preview}")


def _batch_add(
    project: str,
    batch_file: Path,
    agent: str,
    priority: str,
    no_ai: bool,
    dep_list: list[int] | None,
    proj_path: str,
    json_mode: bool,
):
    """批量添加任务."""
    echo(f"[cyan]批量导入: {batch_file}[/cyan]")
    items = _parse_batch_file(batch_file)

    if not items:
        echo("[yellow]文件中没有找到有效任务[/yellow]")
        return

    echo(f"[cyan]将导入 {len(items)} 个任务...[/cyan]")
    click.echo()

    results = []
    for i, item in enumerate(items, 1):
        item_title = item.get("title") or item.get("name") or str(item)
        item_priority = item.get("priority", priority)
        item_agent = _resolve_agent(None, None, item.get("agent", agent))
        item_dep = dep_list  # 批量模式下使用共同的依赖

        echo(f"[dim]{i}/{len(items)}[/dim] {item_title} ", nl=False)
        if no_ai:
            content = ""
        else:
            try:
                content = generate_task_content(
                    item_title, project_path=proj_path, agent=item_agent
                )
            except RuntimeError:
                content = ""

        task = db.create_task(
            project=project,
            title=item_title,
            content=content,
            agent=item_agent,
            priority=item_priority,
            depends_on=item_dep,
        )
        results.append(task)
        echo(f"[green]+ #{task['id']}[/green]")

    if json_mode:
        click.echo(json.dumps({"imported": results, "count": len(results)},
                              ensure_ascii=False, indent=2))
        return

    echo()
    echo(f"[green][OK] 成功导入 {len(results)} 个任务[/green]")
