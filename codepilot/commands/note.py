"""Persistent project notepad commands."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.commands.wiki import _contains_secret
from codepilot.core.output import echo
from codepilot.storage import database as db


NOTE_PATH = Path(".codepilot") / "notepad.md"
SECTION_TITLES = {
    "priority": "Priority Context",
    "working": "Working Memory",
    "manual": "Manual",
}
SECTION_ORDER = ("priority", "working", "manual")
ENTRY_RE = re.compile(r"^- (?P<timestamp>\d{4}-\d{2}-\d{2}T[^|]+) \| (?P<content>.*)$")


class NoteError(ValueError):
    """Raised when a note operation cannot be accepted safely."""


def _resolve_project(project: str | None) -> dict:
    db.init_db()
    if project:
        found = db.get_project(project)
        if not found:
            raise click.ClickException(f"项目 '{project}' 未注册。")
        return found
    found = db.find_project_by_path(Path.cwd())
    if not found:
        raise click.ClickException("当前目录不属于已注册项目；请使用 -p/--project 指定项目。")
    return found


def _note_path(project_info: dict) -> Path:
    return Path(project_info["path"]).resolve() / NOTE_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime | None:
    try:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact_content(content: str) -> str:
    return " ".join(str(content or "").split())


def _empty_notepad(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sections": {key: [] for key in SECTION_ORDER},
    }


def read_notepad(project_info: dict) -> dict[str, Any]:
    """Read and parse the project notepad."""
    path = _note_path(project_info)
    data = _empty_notepad(path)
    if not path.exists():
        return data

    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            title = line.removeprefix("## ").strip()
            current = next((key for key, value in SECTION_TITLES.items() if value == title), None)
            continue
        if current is None or not line.startswith("- "):
            continue
        match = ENTRY_RE.match(line)
        if match:
            data["sections"][current].append(
                {
                    "timestamp": match.group("timestamp").strip(),
                    "content": match.group("content").strip(),
                }
            )
        else:
            data["sections"][current].append({"timestamp": "", "content": line.removeprefix("- ").strip()})
    return data


def _render_notepad(data: dict[str, Any]) -> str:
    lines = [
        "# CodePilot Notepad",
        "",
        "跨会话工作记忆。Priority Context 保持短小；Working Memory 可定期裁剪；Manual 由人工维护。",
        "",
    ]
    sections = data.get("sections") or {}
    for key in SECTION_ORDER:
        lines.append(f"## {SECTION_TITLES[key]}")
        lines.append("")
        entries = list(sections.get(key) or [])
        if entries:
            for item in entries:
                timestamp = str(item.get("timestamp") or "").strip()
                content = _compact_content(str(item.get("content") or ""))
                prefix = f"{timestamp} | " if timestamp else ""
                lines.append(f"- {prefix}{content}")
        else:
            lines.append("-")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_notepad(project_info: dict, data: dict[str, Any]) -> None:
    path = _note_path(project_info)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_notepad(data), encoding="utf-8")


def add_note(project_info: dict, content: str, *, section: str = "working") -> dict[str, Any]:
    """Append one note entry to the selected notepad section."""
    if section not in SECTION_TITLES:
        raise NoteError(f"未知 note section：{section}")
    content = _compact_content(content)
    if not content:
        raise NoteError("note 内容不能为空。")
    if _contains_secret(content):
        raise NoteError("note 不接受疑似 secret、token、password 或 app_secret 内容。")

    data = read_notepad(project_info)
    entry = {"timestamp": _now_iso(), "content": content}
    data["sections"][section].append(entry)
    write_notepad(project_info, data)
    return {"path": _note_path(project_info).name, "section": section, "entry": entry}


def clear_working_notes(project_info: dict) -> dict[str, Any]:
    data = read_notepad(project_info)
    removed = len(data["sections"]["working"])
    data["sections"]["working"] = []
    write_notepad(project_info, data)
    return {"path": _note_path(project_info).name, "removed": removed}


def prune_working_notes(project_info: dict, *, days: int = 7) -> dict[str, Any]:
    if days < 0:
        raise NoteError("--days 不能为负数。")
    data = read_notepad(project_info)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    removed = []
    for entry in data["sections"]["working"]:
        parsed = _parse_timestamp(str(entry.get("timestamp") or ""))
        if parsed is not None and parsed < cutoff:
            removed.append(entry)
        else:
            kept.append(entry)
    data["sections"]["working"] = kept
    write_notepad(project_info, data)
    return {"path": _note_path(project_info).name, "removed": len(removed), "kept": len(kept), "days": days}


def _emit_or_raise(ctx: click.Context, command: str, json_mode: bool, exc: Exception) -> None:
    if json_mode:
        emit_json_payload(command, ok=False, data={}, error=str(exc), error_code="note_error")
        ctx.exit(1)
    raise click.ClickException(str(exc))


@click.group("note")
def note_group() -> None:
    """管理项目持久工作记忆。"""


@note_group.command("add")
@click.argument("content", nargs=-1, required=True)
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--priority", is_flag=True, help="写入 Priority Context")
@click.option("--manual", is_flag=True, help="写入 Manual")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def add_cmd(
    ctx: click.Context,
    content: tuple[str, ...],
    project: str | None,
    priority: bool,
    manual: bool,
    json_mode: bool,
) -> None:
    """新增一条工作记忆。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        if priority and manual:
            raise NoteError("--priority 和 --manual 不能同时使用。")
        section = "priority" if priority else "manual" if manual else "working"
        project_info = _resolve_project(project)
        result = add_note(project_info, " ".join(content), section=section)
    except (NoteError, click.ClickException) as exc:
        _emit_or_raise(ctx, "note add", json_mode, exc)
        return

    data = {"project": project_info["name"], **result}
    if json_mode:
        emit_json_payload("note add", ok=True, data=data)
        return
    echo(f"[green][OK] 已写入 note：{SECTION_TITLES[section]}[/green]")


@note_group.command("show")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def show_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """显示项目 notepad。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    notepad = read_notepad(project_info)
    data = {"project": project_info["name"], **notepad}
    if json_mode:
        emit_json_payload("note show", ok=True, data=data)
        return

    path = _note_path(project_info)
    if not path.exists():
        echo("[yellow]暂无 note[/yellow]")
        return
    click.echo(path.read_text(encoding="utf-8", errors="replace").rstrip())


@note_group.command("clear")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def clear_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """清空 Working Memory，保留 Priority Context 和 Manual。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    result = clear_working_notes(project_info)
    data = {"project": project_info["name"], **result}
    if json_mode:
        emit_json_payload("note clear", ok=True, data=data)
        return
    echo(f"[green][OK] 已清空 Working Memory，移除 {result['removed']} 条[/green]")


@note_group.command("prune")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--days", type=int, default=7, show_default=True, help="移除多少天以前的 Working Memory")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def prune_cmd(ctx: click.Context, project: str | None, days: int, json_mode: bool) -> None:
    """裁剪过期 Working Memory。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        result = prune_working_notes(project_info, days=days)
    except (NoteError, click.ClickException) as exc:
        _emit_or_raise(ctx, "note prune", json_mode, exc)
        return
    data = {"project": project_info["name"], **result}
    if json_mode:
        emit_json_payload("note prune", ok=True, data=data)
        return
    echo(f"[green][OK] 已裁剪 Working Memory，移除 {result['removed']} 条[/green]")
