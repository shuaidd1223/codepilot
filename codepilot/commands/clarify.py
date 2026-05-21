"""Generate a pre-execution requirement specification artifact."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import click

from codepilot.commands.explore import explore_project
from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.output import echo
from codepilot.core.workflow_state import (
    advance_or_update_agent_phase,
    complete_workflow,
    start_workflow,
    update_workflow_state,
    workflow_dirs,
)
from codepilot.storage import database as db


def _now_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _resolve_project(project: str | None) -> dict:
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


def _open_questions(requirement: str, *, quick: bool) -> list[str]:
    questions = [
        "这次改动最重要的成功标准是什么？",
        "哪些文件、模块或用户流程必须纳入范围？",
        "有哪些明确不应该触碰的非目标或兼容性边界？",
    ]
    if not quick:
        questions.extend(
            [
                "是否存在必须保留的现有行为、接口或输出格式？",
                "验收时应该运行哪些命令或检查哪些可观察结果？",
            ]
        )
    if re.search(r"(性能|慢|卡|延迟|timeout|超时)", requirement, re.IGNORECASE):
        questions.append("是否有目标性能指标或可接受的超时阈值？")
    return questions


def _ai_open_questions(
    requirement: str,
    *,
    project_info: dict,
    stream_callback: Callable[[str], None] | None = None,
) -> list[str]:
    if stream_callback is None:
        return []
    try:
        from codepilot.ai_support.clarify import assess_requirement

        assessment = assess_requirement(
            requirement,
            project_path=str(project_info.get("path") or ""),
            config_ref=str(project_info.get("config_file") or project_info.get("path") or ""),
            planner="codex",
            timeout=30,
            stream_callback=stream_callback,
        )
    except Exception:
        return []

    if assessment.get("status") != "needs_clarification":
        return []
    questions: list[str] = []
    for item in assessment.get("questions") or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                questions.append(text)
    return questions


def _collect_evidence(project_info: dict, requirement: str) -> tuple[list[dict[str, Any]], list[str]]:
    limitations: list[str] = []
    try:
        payload = explore_project(requirement, project=str(project_info["name"]))
    except Exception as exc:
        return [], [f"explore 不可用：{exc}"]
    if payload.get("rejected"):
        return [], list(payload.get("limitations") or ["explore 拒绝了该查询。"])
    evidence = list(payload.get("evidence") or [])[:6]
    limitations.extend(str(item) for item in payload.get("limitations") or [])
    return evidence, limitations


def _clarify_next_actions(summary: str, project_info: dict) -> list[dict[str, str]]:
    project_name = project_info.get("name", "<project>")
    return [
        {
            "id": "plan_from_spec",
            "label": "根据当前 clarify spec 生成执行计划",
            "risk": "low",
            "suggested_command": f"codepilot plan -p {project_name} --from-spec <spec_path> --json",
        },
        {
            "id": "submit_requirement",
            "label": "提交为需求并创建 backlog 任务",
            "risk": "medium",
            "suggested_command": f"codepilot go \"{summary}\" -p {project_name} --json",
        },
        {
            "id": "continue_clarify",
            "label": "继续澄清，补充更多细节",
            "risk": "low",
            "suggested_command": f"codepilot clarify -p {project_name} \"补充：...\" --json",
        },
    ]


def _render_evidence(evidence: list[dict[str, Any]], limitations: list[str]) -> str:
    if not evidence:
        lines = ["- 未收集到项目证据。"]
    else:
        lines = []
        for item in evidence:
            title = item.get("title") or item.get("kind") or "evidence"
            summary = str(item.get("summary") or "").strip()
            lines.append(f"- **{title}**：{summary or '无摘要'}")
    if limitations:
        lines.append("")
        lines.append("限制：")
        lines.extend(f"- {item}" for item in limitations[:5])
    return "\n".join(lines)


def build_clarify_spec(
    requirement: str,
    *,
    project_info: dict,
    quick: bool = False,
    stream_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Build a deterministic clarify specification payload."""
    summary = _summary(requirement)
    questions = _open_questions(requirement, quick=quick)
    ai_questions = _ai_open_questions(
        requirement,
        project_info=project_info,
        stream_callback=stream_callback,
    )
    if ai_questions:
        questions = ai_questions + [question for question in questions if question not in ai_questions]
    evidence, limitations = _collect_evidence(project_info, requirement)
    acceptance = [
        "需求范围和非目标已被人工确认。",
        "实现前已有可执行或可人工验证的验收标准。",
        "若需要创建任务，应由后续 plan/go 流程显式执行。",
    ]
    spec = f"""# Clarify Spec: {summary}

## 目标
- 将“{summary}”澄清为执行前可审查规格。

## 范围
- 待确认涉及的代码路径、用户流程、命令或服务。
- 待确认需要保留的现有行为和兼容性要求。

## 非目标
- 本步骤不创建 backlog 任务。
- 本步骤不执行代码、不启动服务、不修改业务代码。

## 约束
- 所有后续实现必须遵守项目现有模块边界和测试策略。
- 涉及 secret、token、账号或外部服务配置时，不输出明文敏感信息。
- brownfield 改动应优先基于本地 evidence，而不是猜测。

## 验收标准
{chr(10).join(f"- {item}" for item in acceptance)}

## 待确认问题
{chr(10).join(f"- {item}" for item in questions)}

## 项目证据
{_render_evidence(evidence, limitations)}
"""
    return {
        "summary": summary,
        "open_questions": questions,
        "acceptance_criteria": acceptance,
        "evidence": evidence,
        "limitations": limitations,
        "spec": spec,
    }


def write_clarify_artifact(
    project_info: dict,
    requirement: str,
    *,
    quick: bool = False,
    stream_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    project_path = Path(project_info["path"]).resolve()
    dirs = workflow_dirs(project_path)
    slug = f"clarify-{_slugify(requirement)}-{_now_slug()}"
    spec_path = dirs["specs"] / f"{slug}.md"
    context_path = dirs["context"] / f"{slug}.json"
    state = start_workflow(
        project_path,
        mode="clarify",
        session_id=slug,
        current_phase="drafting",
        context_path=context_path,
        artifact_paths={"spec": spec_path},
    )
    payload = build_clarify_spec(
        requirement,
        project_info=project_info,
        quick=quick,
        stream_callback=stream_callback,
    )
    next_actions = _clarify_next_actions(payload["summary"], project_info)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(payload["spec"], encoding="utf-8", newline="\n")
    context_path.write_text(
        json.dumps(
            {
                "requirement": requirement,
                "summary": payload["summary"],
                "open_questions": payload["open_questions"],
                "acceptance_criteria": payload["acceptance_criteria"],
                "evidence": payload["evidence"],
                "limitations": payload["limitations"],
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
    complete_workflow(project_path, "clarify")
    final_state = update_workflow_state(project_path, "clarify", next_actions=next_actions)
    advance_or_update_agent_phase(
        project_path,
        "clarify",
        goal=requirement,
        artifact_paths={
            "context": context_path,
            "clarify_context": context_path,
            "spec": spec_path,
        },
        next_actions=next_actions,
        next_action_details=next_actions,
        mode_state=final_state,
    )
    return {
        "project": {"name": project_info["name"], "path": str(project_path)},
        "artifact_path": str(spec_path),
        "context_path": str(context_path),
        "summary": payload["summary"],
        "open_questions": payload["open_questions"],
        "evidence_count": len(payload["evidence"]),
        "mode": "quick" if quick else "standard",
        "next_actions": next_actions,
    }


@click.command("clarify")
@click.argument("requirement", nargs=-1, required=True)
@click.option("--project", "-p", help="项目名称；不指定则按当前目录匹配")
@click.option("--quick/--standard", default=False, help="quick 生成较少问题；standard 为默认")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def clarify(ctx: click.Context, requirement: tuple[str, ...], project: str | None, quick: bool, json_mode: bool) -> None:
    """生成执行前需求规格，不创建任务、不执行代码。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    text = " ".join(requirement).strip()
    if not text:
        raise click.ClickException("需要提供要澄清的需求。")
    project_info = _resolve_project(project)
    streamed = {"seen": False}

    def _stream_chunk(chunk: str) -> None:
        if not chunk:
            return
        streamed["seen"] = True
        click.echo(chunk, nl=False)

    result = write_clarify_artifact(
        project_info,
        text,
        quick=quick,
        stream_callback=None if json_mode else _stream_chunk,
    )
    if json_mode:
        emit_json_payload("clarify", ok=True, data=result)
        return
    if streamed["seen"]:
        click.echo()
    echo(f"[green][OK] 已生成 clarify spec：{result['artifact_path']}[/green]")
    if result["open_questions"]:
        echo("[cyan]待确认问题：[/cyan]")
        for question in result["open_questions"]:
            click.echo(f"- {question}")
