"""codepilot add 命令：添加任务到 backlog（单条或批量）.

支持多种 AI Provider：CLI (claude/codex) 和 API (GPT-4/Claude/混元等)
"""

from __future__ import annotations

import json
import re
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
from codepilot.config import resolve_project_config_reference
from codepilot.output import echo
from codepilot.task_template import missing_task_template_sections


MARKDOWN_BATCH_SUFFIXES = {".md", ".markdown"}


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
    """Resolve agent with fallback.  Returns (agent, fallback_reason, config_ref)."""
    if not value:
        value = "dual"

    project_path = None
    default_mode = "dual"
    if ctx is not None and getattr(ctx, "params", None):
        project_name = ctx.params.get("project")
        project_info = db.get_project(project_name) if project_name else None
        if project_info:
            project_path = resolve_project_config_reference(project_info)
            default_mode = project_info.get("default_mode") or "dual"

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
    解析批量导入文件，支持三种格式：

    1. 每行一个标题（空行和 # 开头的行跳过）：
       优化日志输出
       新增用户认证
       修复登录 bug

    2. JSON 文件（.json 扩展名）：
       [{"title": "...", "priority": "P1", "agent": "claude-sonnet"}, ...]

    3. Markdown 文件（.md/.markdown）：
       每个任务是一份完整的 task-template markdown，多个任务之间用
       后面紧跟下一个一级标题 ``# ...`` 的 ``---`` 分隔。
    """
    content = file_path.read_text(encoding="utf-8").strip()
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        items = json.loads(content)
        return items
    if suffix in MARKDOWN_BATCH_SUFFIXES:
        return _parse_markdown_batch(content)

    # 纯文本格式：每行一个标题
    tasks = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tasks.append({"title": line})
    return tasks


def _json_batch_error(item_index: int, item_title: str, reason: str) -> click.ClickException:
    return click.ClickException(
        f"JSON 批量导入第 {item_index} 项《{item_title}》无效：{reason}。"
        " 请先用 `codepilot ai template --format json` / `--format guide` 生成合规 content。"
    )


def _markdown_batch_error(item_index: int, item_title: str, reason: str) -> click.ClickException:
    return click.ClickException(
        f"Markdown 批量导入第 {item_index} 项《{item_title}》无效：{reason}。"
        " 请确保每个任务都是完整的 task-template markdown，多个任务之间用 `---` 连接，"
        "并让分隔线后紧跟下一个 `# 标题`。"
    )


def _parse_markdown_batch(content: str) -> list[dict]:
    """Parse markdown batch content into task items."""

    text = str(content or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    for index, line in enumerate(lines):
        if _is_markdown_task_boundary(lines, index, current):
            chunk = "\n".join(current).strip()
            if chunk:
                chunks.append(chunk)
            current = []
            continue
        current.append(line)

    tail = "\n".join(current).strip()
    if tail:
        chunks.append(tail)

    items: list[dict] = []
    for chunk in chunks:
        title = _extract_markdown_task_title(chunk)
        item: dict[str, object] = {"title": title, "content": chunk}
        metadata = _extract_markdown_task_metadata(chunk)
        if metadata.get("agent"):
            item["agent"] = metadata["agent"]
        if metadata.get("priority"):
            item["priority"] = metadata["priority"]
        if metadata.get("depends_on") is not None:
            item["depends_on"] = metadata["depends_on"]
        items.append(item)
    return items


def _is_markdown_task_boundary(lines: list[str], index: int, current: list[str]) -> bool:
    """Return True when the current ``---`` line starts the next markdown task."""

    if lines[index].strip() != "---":
        return False
    if not any(part.strip() for part in current):
        return False

    next_index = index + 1
    while next_index < len(lines) and not lines[next_index].strip():
        next_index += 1
    if next_index >= len(lines):
        return False

    remaining = "\n".join(lines[next_index:])
    return re.match(r"^\s*(?:<!--[\s\S]*?-->\s*)*#\s+\S", remaining) is not None


def _extract_markdown_task_title(content: str) -> str:
    match = re.search(r"^\s*#\s+(.+?)\s*$", content, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _extract_markdown_task_metadata(content: str) -> dict[str, object]:
    """Extract agent / priority / depends_on overrides from the Metadata table."""

    match = re.search(
        r"^\s*##\s+Metadata\s*$\n(?P<body>.*?)(?=^\s*---\s*$|^\s*##\s+\S.*$|\Z)",
        content,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if not match:
        return {}

    result: dict[str, object] = {}
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or line.count("|") < 3:
            continue
        cells = [part.strip() for part in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = cells[0].lower()
        value = cells[1]
        if key in {"field", ":---"}:
            continue
        if key == "agent" and value:
            result["agent"] = value
        elif key == "priority" and value:
            result["priority"] = value.upper()
        elif key == "depends on":
            result["depends_on"] = _parse_markdown_depends(value)
    return result


def _parse_markdown_depends(value: str) -> list[int] | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"-", "none", "n/a"} or text == "无":
        return None

    deps: list[int] = []
    seen: set[int] = set()
    for match in re.finditer(r"(?:^|[,，\s])#?(\d+)(?=$|[,，\s])", text):
        task_id = int(match.group(1))
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        deps.append(task_id)
    return deps or None


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
@click.option("--no-ai", is_flag=True, default=False,
              help="跳过 AI 生成。单条/纯文本批量需配合 --allow-empty 才能留空；JSON 批量缺 content 直接拒绝。")
@click.option("--allow-empty", is_flag=True, default=False,
              help="显式允许产生只有标题、无正文的任务（违反模板规范，仅特殊场景使用）。")
@click.option("--depends", "depends_on", default="", help="依赖的任务 ID，多个用逗号分隔")
@click.option("--file", "-f", "batch_file", type=click.Path(exists=True, path_type=Path),
              help="从文件批量导入任务（每行一个标题，或 .json / .md 格式）")
@click.option("--json", "json_mode", is_flag=True, hidden=True, help="JSON 输出")
@click.pass_context
def add(
    ctx: click.Context,
    project: str,
    title: str | None,
    agent: str,
    priority: str,
    no_ai: bool,
    allow_empty: bool,
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
      codepilot add -p myproj -f tasks.md
    """
    db.init_db()
    # 优先用本地 --json，否则用全局
    if not json_mode and ctx.parent:
        json_mode = ctx.parent.obj.get("json_mode", False)
    proj_info = db.get_project(project)
    proj_path = proj_info.get("path", "") if proj_info else ""
    config_ref = resolve_project_config_reference(proj_info) if proj_info else proj_path

    # 解析依赖
    dep_list: list[int] | None = None
    if depends_on:
        raw_ids = [d.strip() for d in depends_on.split(",") if d.strip()]
        dep_list = [int(d) for d in raw_ids if d.isdigit()]

    # 批量模式
    if batch_file:
        return _batch_add(
            project, batch_file, agent, priority, no_ai, dep_list,
            proj_path, json_mode, config_ref=config_ref,
        )

    # 单条模式
    if not title:
        raise click.BadParameter("--title 或 --file 必须指定一个")

    # Re-resolve with fallback info (the Click callback doesn't propagate it).
    default_mode = proj_info.get("default_mode", "dual") if proj_info else "dual"
    effective_agent, fallback_reason, _ = _resolve_agent_info(ctx, agent)

    _single_add(
        project, title, effective_agent, priority, no_ai, dep_list, proj_path,
        json_mode, fallback_reason=fallback_reason, config_ref=config_ref,
        allow_empty=allow_empty,
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
    config_ref: str | Path | None = None,
    allow_empty: bool = False,
):
    """添加单个任务."""
    if fallback_reason:
        echo(f"[yellow]⚠ {fallback_reason}[/yellow]")

    if no_ai:
        if not allow_empty:
            raise click.ClickException(
                f"--no-ai 单条添加会产生只有标题《{title}》、无正文的任务，"
                "违反任务模板规范（外部添加的任务必须含 Task Goal / Acceptance Criteria 等关键章节）。\n"
                "正确做法：\n"
                "  - 让 CodePilot 自己规划：python -m codepilot \"需求描述\"\n"
                "  - 让 AI 生成 content：去掉 --no-ai\n"
                "  - 仅在确实需要占位时显式加 --allow-empty\n"
                "字段规范见：python -m codepilot ai template --format guide"
            )
        content = ""
        echo("[yellow]跳过 AI 生成，内容为空（--allow-empty 已显式允许，违反模板规范）[/yellow]")
    else:
        echo(f"[cyan]调用 {agent} 生成任务内容...[/cyan]")
        content = generate_task_content(
            title,
            project_path=proj_path,
            agent=agent,
            config_ref=config_ref,
        )
        if not allow_empty:
            missing = missing_task_template_sections(content)
            if missing:
                raise click.ClickException(
                    f"AI 生成的任务《{title}》正文缺少模板必需章节: {', '.join(missing)}。\n"
                    "可能原因：generate_task_content 的提示模板还没对齐 task-template.md，"
                    "或当前 agent 未按提示渲染章节。\n"
                    "处理建议：换一个 agent 重试；或先用 ai template --format guide 自查章节，"
                    "手工补全后再投递；确认占位场景可加 --allow-empty 跳过校验。"
                )
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
    config_ref: str | Path | None = None,
):
    """批量添加任务."""
    echo(f"[cyan]批量导入: {batch_file}[/cyan]")
    items = _parse_batch_file(batch_file)
    batch_suffix = batch_file.suffix.lower()
    is_json_batch = batch_suffix == ".json"
    is_markdown_batch = batch_suffix in MARKDOWN_BATCH_SUFFIXES

    if not items:
        echo("[yellow]文件中没有找到有效任务[/yellow]")
        return

    echo(f"[cyan]将导入 {len(items)} 个任务...[/cyan]")
    click.echo()

    prepared_items: list[dict[str, object]] = []
    for i, item in enumerate(items, 1):
        item_title = item.get("title") or item.get("name") or str(item)
        item_priority = item.get("priority", priority)
        item_agent = _resolve_agent(None, None, item.get("agent", agent))

        # 允许每条任务覆盖全局 --depends（JSON 中 depends/depends_on/dependsOn 均可）
        item_dep_raw = (
            item.get("depends")
            if "depends" in item
            else item.get("depends_on")
            if "depends_on" in item
            else item.get("dependsOn")
        )
        if item_dep_raw is not None:
            if isinstance(item_dep_raw, (list, tuple)):
                item_dep = [int(x) for x in item_dep_raw if str(x).strip()]
            elif isinstance(item_dep_raw, str):
                item_dep = [int(x.strip()) for x in item_dep_raw.split(",") if x.strip().isdigit()]
            else:
                item_dep = [int(item_dep_raw)]
        else:
            item_dep = dep_list  # 回退到批量命令行 --depends

        # 用户若在 JSON 里直接给出 content / body / description，就优先采用，
        # 不再触发 AI 生成或被 --no-ai 清空。
        user_content = item.get("content") or item.get("body") or item.get("description")

        echo(f"[dim]{i}/{len(items)}[/dim] {item_title} ", nl=False)
        if isinstance(user_content, str) and user_content.strip():
            content = user_content
            if is_json_batch or is_markdown_batch:
                missing = missing_task_template_sections(content)
                if missing:
                    error_factory = _json_batch_error if is_json_batch else _markdown_batch_error
                    raise error_factory(
                        i,
                        item_title,
                        f"content 缺少关键章节: {', '.join(missing)}",
                    )
        elif no_ai:
            if is_json_batch:
                raise _json_batch_error(
                    i,
                    item_title,
                    "缺少 content，且当前使用了 --no-ai，导入会产生只有标题的空任务",
                )
            if is_markdown_batch:
                raise _markdown_batch_error(
                    i,
                    item_title,
                    "缺少可导入的 markdown 任务正文",
                )
            content = ""
        else:
            if is_markdown_batch:
                raise _markdown_batch_error(
                    i,
                    item_title,
                    "缺少可导入的 markdown 任务正文",
                )
            try:
                content = generate_task_content(
                    item_title,
                    project_path=proj_path,
                    agent=item_agent,
                    config_ref=config_ref,
                )
            except RuntimeError as exc:
                if is_json_batch:
                    raise _json_batch_error(
                        i,
                        item_title,
                        f"AI 生成任务内容失败: {exc}",
                    ) from exc
                content = ""

            if is_json_batch:
                missing = missing_task_template_sections(content)
                if missing:
                    raise _json_batch_error(
                        i,
                        item_title,
                        f"AI 生成的 content 缺少关键章节: {', '.join(missing)}",
                    )

        prepared_items.append(
            {
                "title": item_title,
                "content": content,
                "agent": item_agent,
                "priority": item_priority,
                "depends_on": item_dep,
            }
        )
        echo("[green]ok[/green]")

    results = []
    for item in prepared_items:
        task = db.create_task(
            project=project,
            title=str(item["title"]),
            content=str(item["content"]),
            agent=str(item["agent"]),
            priority=str(item["priority"]),
            depends_on=item.get("depends_on"),
        )
        results.append(task)
        echo(f"[green]+ #{task['id']}[/green]")

    if json_mode:
        click.echo(json.dumps({"imported": results, "count": len(results)},
                              ensure_ascii=False, indent=2))
        return

    echo()
    echo(f"[green][OK] 成功导入 {len(results)} 个任务[/green]")
