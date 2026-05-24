"""Generate a reviewable execution plan artifact without creating tasks."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from codepilot.ai_support.service import build_task_markdown_from_plan
from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.output import echo
from codepilot.core.workflow_state import (
    advance_or_update_agent_phase,
    complete_workflow,
    start_workflow,
    update_workflow_state,
    workflow_dirs,
)


def _now_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _resolve_project(project: str | None) -> dict:
    from codepilot.storage import database as db
    db.init_db()
    if project:
        found = db.get_project(project)
        if not found:
            raise click.ClickException(f"项目 '{project}' 未注册。")
        return found
    found = db.find_project_by_path(Path.cwd())
    if found:
        return found
    projects = db.list_projects()
    if len(projects) == 1:
        return projects[0]
    raise click.ClickException("当前目录不属于已注册项目；请使用 -p/--project 指定项目。")


def _slugify(text: str) -> str:
    tokens = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", text or "")
    slug = "-".join(token.lower()[:16] for token in tokens[:4]).strip("-")
    return slug or "requirement"


def _summary(requirement: str) -> str:
    compact = re.sub(r"\s+", " ", requirement.strip())
    return compact[:160] if compact else "待澄清需求"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _extract_section(text: str, heading: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    next_heading = re.search(r"^##\s+", text[match.end() :], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(text)
    return text[match.end() : end].strip()


def _read_spec(project_path: Path, from_spec: str) -> tuple[Path, str]:
    candidate = Path(from_spec).expanduser()
    if not candidate.is_absolute():
        candidate = project_path / candidate
    spec_path = candidate.resolve()
    if not spec_path.is_relative_to(project_path):
        raise click.ClickException("--from-spec 必须指向项目目录内的文件。")
    if not spec_path.is_file():
        raise click.ClickException(f"spec 文件不存在：{spec_path}")
    return spec_path, spec_path.read_text(encoding="utf-8", errors="replace")


def _summary_from_spec(spec_text: str) -> str:
    title = re.search(r"^#\s+Clarify Spec:\s*(.+?)\s*$", spec_text, re.MULTILINE)
    if title and title.group(1).strip():
        return _summary(title.group(1))
    goal = _extract_section(spec_text, "目标")
    if goal:
        compact = re.sub(r"^[\s\-*]+", "", goal.splitlines()[0]).strip()
        return _summary(compact)
    return _summary(spec_text)


def _topic_files(requirement: str) -> list[str]:
    lowered = requirement.lower()
    files: list[str] = []
    rules = [
        (("doctor", "健康", "诊断"), ["codepilot/commands/doctor.py", "tests/test_doctor_command.py", "docs/操作文档.zh-CN.md"]),
        (("explore", "探索", "证据"), ["codepilot/commands/explore.py", "tests/test_explore_command.py"]),
        (("clarify", "澄清", "规格"), ["codepilot/commands/clarify.py", "tests/test_clarify_command.py"]),
        (("plan", "计划", "规划"), ["codepilot/commands/plan.py", "tests/test_plan_command.py"]),
        (("wiki", "memory", "知识"), ["codepilot/commands/wiki.py", "tests/test_wiki_command.py"]),
        (("ui", "web", "面板"), ["codepilot/commands/ui.py", "codepilot/commands/webui_service.py", "tests/"]),
        (("feishu", "飞书"), ["codepilot/commands/feishu.py", "tests/"]),
    ]
    for terms, candidates in rules:
        if any(term in lowered or term in requirement for term in terms):
            files.extend(candidates)
    if not files:
        files.extend(["待确认相关命令或模块", "tests/"])
    return _dedupe(files)


def _candidate_title(summary: str, index: int) -> str:
    if index == 1:
        if re.search(r"(修复|bug|失败|报错|fix)", summary, re.IGNORECASE):
            return f"修复 {summary} 的核心行为"
        if re.search(r"(新增|添加|支持|实现|add)", summary, re.IGNORECASE):
            return f"实现 {summary}"
        return f"完成 {summary} 的核心改动"
    return f"补齐 {summary} 的验证与文档"


def _build_task_candidates(summary: str, requirement: str, files: list[str]) -> list[dict[str, Any]]:
    primary_files = files[:3]
    test_files = [item for item in files if item.startswith("tests/")] or ["tests/"]
    return [
        {
            "id": "T1",
            "title": _candidate_title(summary, 1),
            "goal": f"在不启动执行器、不创建 backlog 的前提下，明确并实现「{summary}」的最小可交付行为。",
            "files": primary_files,
            "acceptance_criteria": [
                "实现范围与计划 artifact 中的文件边界一致。",
                "默认路径不会创建任务、启动 daemon 或执行 run。",
            ],
        },
        {
            "id": "T2",
            "title": _candidate_title(summary, 2),
            "goal": f"为「{summary}」补齐 focused tests、JSON contract 验证和必要文档。",
            "files": _dedupe(test_files + ["AI_MANIFEST.json", "docs/AI与Agent调用手册.zh-CN.md"]),
            "acceptance_criteria": [
                "focused tests 覆盖文本输入和 artifact 输出。",
                "回归测试通过，文档说明默认不入 backlog 的边界。",
            ],
        },
    ]


def _build_risks(requirement: str, *, source: str) -> list[str]:
    risks = [
        "计划 artifact 只是审批层，导入任务前仍需人工确认范围和优先级。",
        "候选任务基于本地规则生成，复杂依赖关系需要执行前复核。",
    ]
    if source == "spec":
        risks.append("clarify spec 中的待确认问题若未回答，计划可能仍保留不确定性。")
    if re.search(r"(db|database|schema|数据库|迁移|auth|认证)", requirement, re.IGNORECASE):
        risks.append("需求可能触及数据库、认证或兼容性边界，需要提高验证范围。")
    return risks


def _build_verification_plan(summary: str, files: list[str]) -> list[dict[str, str]]:
    keyword = "plan_command"
    for candidate in ("doctor", "explore", "clarify", "wiki"):
        if any(candidate in item.lower() for item in files) or candidate in summary.lower():
            keyword = candidate
            break
    return [
        {
            "criterion": "计划 artifact 已生成且 JSON contract 稳定。",
            "command": "pytest tests -k plan_command -q",
            "expected": "新增 plan 命令 focused tests 通过。",
        },
        {
            "criterion": "相关功能无明显回归。",
            "command": f"pytest tests -k {keyword} -q",
            "expected": "目标相关测试通过。",
        },
        {
            "criterion": "全量回归。",
            "command": "pytest tests -q",
            "expected": "现有测试不回归。",
        },
    ]


def _read_json_object(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe([str(item) for item in value if str(item).strip()])


def _inspect_candidate_items(context: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ("created_preview", "report_only"):
        raw_items = context.get(key)
        if isinstance(raw_items, list):
            items.extend(dict(item) for item in raw_items if isinstance(item, dict))
    return items


def _build_inspect_task_candidates(context: dict[str, Any], summary: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(_inspect_candidate_items(context), 1):
        title = str(item.get("title") or _candidate_title(summary, index)).strip()
        goal = str(item.get("goal") or item.get("rationale") or f"完成「{title}」。").strip()
        files = _text_list(item.get("files")) or ["待确认相关命令或模块"]
        acceptance = _text_list(item.get("acceptance_criteria")) or ["完成巡检候选项描述的核心行为。"]
        candidate: dict[str, Any] = {
            "id": f"T{index}",
            "title": title,
            "goal": goal,
            "files": files,
            "acceptance_criteria": acceptance,
        }
        priority = str(item.get("priority") or "").strip().upper()
        if priority in {"P0", "P1", "P2", "P3"}:
            candidate["priority"] = priority
        agent = str(item.get("agent") or "").strip()
        if agent:
            candidate["agent"] = agent
        evidence = str(item.get("evidence") or "").strip()
        if evidence:
            candidate["evidence"] = evidence
        verification = _text_list(item.get("verification_commands"))
        if verification:
            candidate["verification_commands"] = verification
        candidates.append(candidate)
    return candidates


def _build_inspect_verification_plan(
    candidates: list[dict[str, Any]],
    summary: str,
    files: list[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "T?").strip()
        for command in _text_list(candidate.get("verification_commands")):
            if command in seen:
                continue
            seen.add(command)
            rows.append(
                {
                    "criterion": f"{candidate_id} 巡检候选验证。",
                    "command": command,
                    "expected": "命令通过，确认巡检候选无回归。",
                }
            )
    return rows or _build_verification_plan(summary, files)


def _build_inspect_execution_plan(
    requirement: str,
    *,
    source_path: str | None,
    wiki_context: dict[str, Any] | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    summary = _summary(str(context.get("summary") or requirement))
    candidates = _build_inspect_task_candidates(context, summary)
    if candidates:
        files = _dedupe([file for candidate in candidates for file in candidate.get("files", [])])
        verification_plan = _build_inspect_verification_plan(candidates, summary, files)
    else:
        files = _topic_files(requirement)
        candidates = _build_task_candidates(summary, requirement, files)
        verification_plan = _build_verification_plan(summary, files)
    payload: dict[str, Any] = {
        "summary": summary,
        "source": "inspect",
        "source_path": source_path,
        "source_label": source_path or summary,
        "files": files,
        "task_candidates": candidates,
        "risks": _build_risks(requirement, source="inspect"),
        "verification_plan": verification_plan,
        "wiki_context": wiki_context or {"enabled": False, "query": requirement, "results": []},
    }
    payload["plan"] = _render_plan_markdown(payload)
    return payload


def _command_path(path: str | None, placeholder: str) -> str:
    text = str(path or "").strip() or placeholder
    if re.search(r"\s", text):
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _plan_next_actions(
    summary: str,
    project_info: dict,
    plan_path: str | None = None,
    task_batch_path: str | None = None,
) -> list[dict[str, str]]:
    project_name = project_info.get("name", "<project>")
    task_batch_arg = _command_path(task_batch_path, "<task_batch_path>")
    return [
        {
            "id": "import_tasks",
            "label": "将候选任务导入 backlog",
            "risk": "medium",
            "suggested_command": f"codepilot add -p {project_name} -f {task_batch_arg}",
        },
        {
            "id": "replan",
            "label": "重新规划以细化或补充内容",
            "risk": "low",
            "suggested_command": f"codepilot plan -p {project_name} \"{summary}\" --json",
        },
        {
            "id": "execute_directly",
            "label": "直接执行计划",
            "risk": "high",
            "suggested_command": f"codepilot run -p {project_name} --once --json",
        },
        {
            "id": "abandon_plan",
            "label": "放弃该计划，删除 plan artifact",
            "risk": "low",
            "suggested_command": f"rm {_command_path(plan_path, '<plan_path>')}",
        },
    ]


def _render_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 待确认"


def _render_plan_markdown(payload: dict[str, Any]) -> str:
    candidates = payload["task_candidates"]
    verification = payload["verification_plan"]
    candidate_blocks: list[str] = []
    for candidate in candidates:
        candidate_lines = [
            f"### {candidate['id']}. {candidate['title']}",
            "",
            f"- 目标：{candidate['goal']}",
            "- 文件范围：",
            _render_list(candidate["files"]),
            "- 验收标准：",
            _render_list(candidate["acceptance_criteria"]),
        ]
        evidence = str(candidate.get("evidence") or "").strip()
        if evidence:
            candidate_lines.extend(["- 证据：", _render_list([evidence])])
        candidate_blocks.append("\n".join(candidate_lines))
    verification_rows = "\n".join(
        f"| {item['criterion']} | `{item['command']}` | {item['expected']} |" for item in verification
    )
    order_lines = "\n".join(f"{idx + 1}. {item['title']}" for idx, item in enumerate(candidates))
    wiki_results = (payload.get("wiki_context") or {}).get("results") or []
    wiki_lines = "\n".join(f"- {item['path']}：{item['title'] or item['summary']}" for item in wiki_results) or "- 未匹配到 wiki 引用。"
    return f"""# Execution Plan: {payload['summary']}

## 来源
- 类型：{payload['source']}
- 输入：{payload['source_label']}

## 执行顺序
{order_lines}

## 文件范围
{_render_list(payload['files'])}

## Wiki 引用
{wiki_lines}

## 风险
{_render_list(payload['risks'])}

## 验证矩阵
| Criterion | Command | Expected Result |
| :--- | :--- | :--- |
{verification_rows}

## 任务候选
{chr(10).join(candidate_blocks)}

## 后续选择
- 导入任务：人工确认后，用 `codepilot add` 或后续导入流程创建 backlog。
- 继续 clarify：如果风险或范围仍不清晰，先运行 `codepilot clarify "{payload['summary']}" --json`。
- 放弃：如果计划不成立，删除本 artifact 即可；本命令未创建任务、未启动执行。
"""


def build_execution_plan(
    requirement: str,
    *,
    source: str = "text",
    source_path: str | None = None,
    wiki_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if source == "inspect":
        context = _read_json_object(source_path)
        if context:
            return _build_inspect_execution_plan(
                requirement,
                source_path=source_path,
                wiki_context=wiki_context,
                context=context,
            )
    summary = _summary(requirement)
    files = _topic_files(requirement)
    candidates = _build_task_candidates(summary, requirement, files)
    risks = _build_risks(requirement, source=source)
    verification_plan = _build_verification_plan(summary, files)
    payload: dict[str, Any] = {
        "summary": summary,
        "source": source,
        "source_path": source_path,
        "source_label": source_path or summary,
        "files": files,
        "task_candidates": candidates,
        "risks": risks,
        "verification_plan": verification_plan,
        "wiki_context": wiki_context or {"enabled": False, "query": requirement, "results": []},
    }
    payload["plan"] = _render_plan_markdown(payload)
    return payload


def _task_batch_item_from_candidate(candidate: dict[str, Any], payload: dict[str, Any], index: int) -> dict[str, str]:
    title = str(candidate.get("title") or f"计划候选任务 {index}").strip()
    priority = str(candidate.get("priority") or "P2").strip().upper()
    if priority not in {"P0", "P1", "P2", "P3"}:
        priority = "P2"
    agent = str(candidate.get("agent") or "dual").strip() or "dual"
    files = [str(item) for item in candidate.get("files") or [] if str(item).strip()]
    summary = str(payload.get("summary") or "计划任务").strip()
    candidate_id = str(candidate.get("id") or f"T{index}").strip()
    acceptance = [
        str(item)
        for item in candidate.get("acceptance_criteria") or []
        if str(item).strip()
    ]
    template_task = {
        "title": title,
        "priority": priority,
        "agent": agent,
        "risk_level": "中",
        "scope_budget": f"最多 {max(len(files), 1)} 个文件 / 小范围改动",
        "owner": "未指派",
        "goal": str(candidate.get("goal") or f"完成「{summary}」中的 {candidate_id}。").strip(),
        "acceptance_criteria": acceptance,
        "builder_notes": [
            f"完成 plan artifact 中 {candidate_id} 的目标。",
            "沿用现有模块边界、命名和测试风格。",
        ],
        "reviewer_notes": [
            "确认任务正文满足 task-template 必需章节。",
            "确认验收标准和验证命令覆盖本任务范围。",
        ],
        "files": files,
        "notes": [str(item) for item in payload.get("risks") or [] if str(item).strip()],
        "forbidden": [
            "不要自动导入 backlog 或启动执行器。",
            "不要提交密钥、令牌、个人配置或生成产物。",
            "不要改动本候选任务范围外的模块。",
        ],
        "not_in_scope": [
            "不处理 plan artifact 未列出的扩展需求。",
            "不做与本候选任务无关的重构、格式化或依赖升级。",
        ],
        "evidence": (
            f"来自 plan artifact「{summary}」的任务候选 {candidate_id}；"
            f"文件范围：{', '.join(files) if files else '待确认'}。"
        ),
    }
    content = build_task_markdown_from_plan(template_task, language="zh-CN")
    return {
        "title": title,
        "priority": priority,
        "agent": agent,
        "content": content,
    }


def _build_task_batch_items(payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        _task_batch_item_from_candidate(candidate, payload, index)
        for index, candidate in enumerate(payload.get("task_candidates") or [], 1)
        if isinstance(candidate, dict)
    ]


def write_plan_artifact(
    project_info: dict,
    requirement: str,
    *,
    source: str = "text",
    source_path: str | None = None,
    use_wiki: bool = True,
) -> dict[str, Any]:
    project_path = Path(project_info["path"]).resolve()
    dirs = workflow_dirs(project_path)
    slug = f"plan-{_slugify(requirement)}-{_now_slug()}"
    plan_path = dirs["plans"] / f"{slug}.md"
    context_path = dirs["context"] / f"{slug}.json"
    task_batch_path = dirs["context"] / f"{slug}.tasks.json"
    state = start_workflow(
        project_path,
        mode="plan",
        session_id=slug,
        current_phase="drafting",
        context_path=context_path,
        artifact_paths={"plan": plan_path, "task_batch": task_batch_path},
    )
    from codepilot.commands.wiki import wiki_context as collect_wiki_context

    wiki = collect_wiki_context(project_info, requirement, enabled=use_wiki, limit=5)
    payload = build_execution_plan(requirement, source=source, source_path=source_path, wiki_context=wiki)
    task_batch_items = _build_task_batch_items(payload)
    next_actions = _plan_next_actions(
        payload["summary"],
        project_info,
        plan_path=str(plan_path),
        task_batch_path=str(task_batch_path),
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(payload["plan"], encoding="utf-8", newline="\n")
    task_batch_path.write_text(
        json.dumps(task_batch_items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    context_path.write_text(
        json.dumps(
            {
                "requirement": requirement,
                "summary": payload["summary"],
                "source": payload["source"],
                "source_path": payload["source_path"],
                "files": payload["files"],
                "task_candidates": payload["task_candidates"],
                "risks": payload["risks"],
                "verification_plan": payload["verification_plan"],
                "wiki_context": payload["wiki_context"],
                "task_batch_path": str(task_batch_path),
                "next_actions": next_actions,
                "state": state,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    complete_workflow(project_path, "plan")
    final_state = update_workflow_state(project_path, "plan", next_actions=next_actions)
    agent_artifacts: dict[str, str | Path] = {
        "context": context_path,
        "plan_context": context_path,
        "plan": plan_path,
        "task_batch": task_batch_path,
    }
    if source_path:
        agent_artifacts["spec"] = source_path
    advance_or_update_agent_phase(
        project_path,
        "plan",
        goal=requirement,
        artifact_paths=agent_artifacts,
        next_actions=next_actions,
        next_action_details=next_actions,
        mode_state=final_state,
    )
    return {
        "project": {"name": project_info["name"], "path": str(project_path)},
        "plan_path": str(plan_path),
        "context_path": str(context_path),
        "task_batch_path": str(task_batch_path),
        "summary": payload["summary"],
        "source": payload["source"],
        "source_path": payload["source_path"],
        "files": payload["files"],
        "task_candidates": payload["task_candidates"],
        "risks": payload["risks"],
        "verification_plan": payload["verification_plan"],
        "wiki_context": payload["wiki_context"],
        "next_actions": next_actions,
    }


@click.command("plan")
@click.argument("requirement", nargs=-1, required=False)
@click.option("--project", "-p", help="项目名称；不指定则按当前目录匹配")
@click.option("--from-spec", "from_spec", type=click.Path(path_type=str), help="从项目内 clarify spec 生成计划")
@click.option("--use-wiki/--no-wiki", default=True, show_default=True, help="是否读取项目 wiki 作为只读上下文")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def plan(
    ctx: click.Context,
    requirement: tuple[str, ...],
    project: str | None,
    from_spec: str | None,
    use_wiki: bool,
    json_mode: bool,
) -> None:
    """生成可审查执行计划，不创建 backlog、不启动执行。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    text = " ".join(requirement).strip()
    if from_spec and text:
        raise click.ClickException("不能同时提供需求文本和 --from-spec。")
    if not from_spec and not text:
        raise click.ClickException("需要提供需求文本，或使用 --from-spec 指向 clarify spec。")

    project_info = _resolve_project(project)
    source = "text"
    source_path: str | None = None
    if from_spec:
        spec_path, spec_text = _read_spec(Path(project_info["path"]).resolve(), from_spec)
        text = _summary_from_spec(spec_text)
        source = "spec"
        source_path = str(spec_path)

    result = write_plan_artifact(project_info, text, source=source, source_path=source_path, use_wiki=use_wiki)
    if json_mode:
        emit_json_payload("plan", ok=True, data=result)
        return

    echo(f"[green][OK] 已生成 plan artifact：{result['plan_path']}[/green]")
    echo("[cyan]任务候选：[/cyan]")
    for item in result["task_candidates"]:
        click.echo(f"- {item['id']} {item['title']}")
