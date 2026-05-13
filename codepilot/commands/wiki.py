"""Markdown-first project wiki commands."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from codepilot.commands.json_contract import emit_json_payload, resolve_json_mode
from codepilot.core.output import echo
from codepilot.storage import database as db


WIKI_DIR = Path(".codepilot") / "wiki"
MAX_QUERY_RESULTS = 20
MAX_SUMMARY_CHARS = 180
SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$")
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|app[_-]?secret|client[_-]?secret|token|password)\b\s*[=:]\s*\S+"),
    re.compile(r"(?i)\b(feishu[_-]?app[_-]?secret)\b"),
)


class WikiError(ValueError):
    """Raised when wiki input cannot be accepted safely."""


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


def _wiki_dir(project_info: dict) -> Path:
    return Path(project_info["path"]).resolve() / WIKI_DIR


def _slugify(title: str) -> str:
    ascii_slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", title.strip().lower()).strip("-._")
    if ascii_slug and SAFE_SLUG_RE.match(ascii_slug):
        return ascii_slug[:80]
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", title))
    if cjk:
        return "-".join(re.findall(r"[\u4e00-\u9fff]{1,12}", cjk))[:80]
    return "note"


def _validate_slug(slug: str) -> str:
    normalized = (slug or "").strip().replace("\\", "/")
    if "/" in normalized or normalized in {"", ".", ".."} or ".." in normalized:
        raise WikiError("非法 wiki 路径：slug 不允许包含路径分隔符或上级目录。")
    if not (SAFE_SLUG_RE.match(normalized) or re.fullmatch(r"[\u4e00-\u9fff][\u4e00-\u9fff._-]{0,79}", normalized)):
        raise WikiError("非法 wiki 路径：slug 只能包含字母、数字、中文、点、下划线或短横线。")
    return normalized


def _page_path(wiki_dir: Path, slug: str) -> Path:
    safe_slug = _validate_slug(slug)
    path = (wiki_dir / f"{safe_slug}.md").resolve()
    root = wiki_dir.resolve()
    if root not in path.parents:
        raise WikiError("非法 wiki 路径：页面必须位于项目 .codepilot/wiki 内。")
    return path


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in SECRET_PATTERNS)


def _frontmatter(metadata: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value)
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines)


def _parse_page(path: Path, wiki_dir: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = path.resolve().relative_to(wiki_dir.resolve()).as_posix()
    metadata: dict[str, str] = {}
    body = text
    has_metadata = False
    if text.startswith("---\n"):
        parts = text.split("\n---\n", 1)
        if len(parts) == 2:
            has_metadata = True
            raw_meta = parts[0].removeprefix("---\n")
            body = parts[1]
            for line in raw_meta.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()
    return {
        "path": rel,
        "slug": path.stem,
        "title": metadata.get("title", ""),
        "created_at": metadata.get("created_at", ""),
        "source": metadata.get("source", ""),
        "tags": [item.strip() for item in metadata.get("tags", "").split(",") if item.strip()],
        "body": body.strip(),
        "has_metadata": has_metadata,
    }


def add_wiki_note(
    project_info: dict,
    *,
    title: str,
    body: str,
    slug: str | None = None,
    source: str = "manual",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a wiki note in the project's local wiki directory."""
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        raise WikiError("wiki 页面标题不能为空。")
    if not body:
        raise WikiError("wiki 页面正文不能为空。")
    if _contains_secret(f"{title}\n{body}"):
        raise WikiError("wiki 不接受疑似 secret、token、password 或 app_secret 内容。")

    wiki_dir = _wiki_dir(project_info)
    wiki_dir.mkdir(parents=True, exist_ok=True)
    base_slug = _validate_slug(slug) if slug else _slugify(title)
    path = _page_path(wiki_dir, base_slug)
    if path.exists() and not slug:
        suffix = 2
        while path.exists():
            path = _page_path(wiki_dir, f"{base_slug}-{suffix}")
            suffix += 1
    elif path.exists():
        raise WikiError(f"wiki 页面已存在：{path.name}")

    extra_metadata = dict(metadata or {})
    page_metadata = {
        "title": title,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": source or "manual",
        "tags": tags or [],
    }
    for key, value in extra_metadata.items():
        safe_key = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(key or "").strip()).strip("_")
        if safe_key and safe_key not in page_metadata:
            page_metadata[safe_key] = value
    path.write_text(f"{_frontmatter(page_metadata)}\n\n{body}\n", encoding="utf-8")
    return {"path": path.name, "title": title, "source": page_metadata["source"], "tags": page_metadata["tags"]}


def list_wiki_pages(project_info: dict) -> list[dict[str, Any]]:
    wiki_dir = _wiki_dir(project_info)
    if not wiki_dir.exists():
        return []
    pages = []
    for path in sorted(wiki_dir.glob("*.md")):
        page = _parse_page(path, wiki_dir)
        pages.append({key: page[key] for key in ("path", "slug", "title", "created_at", "source", "tags")})
    return pages


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    tokens = re.findall(r"[a-z0-9_./-]{2,}|[\u4e00-\u9fff]{1,}", lowered)
    cjk_bigrams: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        cjk_bigrams.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    seen: list[str] = []
    for token in tokens + cjk_bigrams:
        if token not in seen:
            seen.append(token)
    return seen


def _summary(body: str) -> str:
    compact = re.sub(r"\s+", " ", body.strip())
    if len(compact) <= MAX_SUMMARY_CHARS:
        return compact
    return compact[: MAX_SUMMARY_CHARS - 3].rstrip() + "..."


def query_wiki(project_info: dict, query: str, *, limit: int = MAX_QUERY_RESULTS) -> list[dict[str, Any]]:
    query_terms = _tokens(query)
    if not query_terms:
        return []
    wiki_dir = _wiki_dir(project_info)
    if not wiki_dir.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(wiki_dir.glob("*.md")):
        page = _parse_page(path, wiki_dir)
        haystack = f"{page['title']}\n{' '.join(page['tags'])}\n{page['body']}".lower()
        score = sum(3 if term in page["title"].lower() else 1 for term in query_terms if term in haystack)
        if score <= 0:
            continue
        results.append(
            {
                "path": page["path"],
                "title": page["title"],
                "summary": _summary(page["body"]),
                "score": score,
            }
        )
    return sorted(results, key=lambda item: (-item["score"], item["path"]))[:limit]


def wiki_context(project_info: dict, query: str, *, enabled: bool = True, limit: int = 5) -> dict[str, Any]:
    """Return small read-only wiki context for agent/provider-neutral workflows."""
    if not enabled:
        return {"enabled": False, "query": query, "results": []}
    return {"enabled": True, "query": query, "results": query_wiki(project_info, query, limit=limit)}


def update_wiki_page(
    project_info: dict,
    *,
    slug: str,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    wiki_dir = _wiki_dir(project_info)
    path = _page_path(wiki_dir, slug)
    if not path.is_file():
        raise WikiError(f"wiki 页面不存在：{slug}.md")
    existing = _parse_page(path, wiki_dir)
    next_title = (title if title is not None else existing["title"]).strip()
    next_body = (body if body is not None else existing["body"]).strip()
    next_tags = tags if tags is not None else existing["tags"]
    if not next_title:
        raise WikiError("wiki 页面标题不能为空。")
    if not next_body:
        raise WikiError("wiki 页面正文不能为空。")
    if _contains_secret(f"{next_title}\n{next_body}"):
        raise WikiError("wiki 不接受疑似 secret、token、password 或 app_secret 内容。")
    metadata = {
        "title": next_title,
        "created_at": existing["created_at"] or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": existing["source"] or "manual",
        "tags": next_tags,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    path.write_text(f"{_frontmatter(metadata)}\n\n{next_body}\n", encoding="utf-8")
    return {"path": path.name, "title": next_title, "source": metadata["source"], "tags": next_tags}


def delete_wiki_page(project_info: dict, *, slug: str) -> dict[str, Any]:
    wiki_dir = _wiki_dir(project_info)
    path = _page_path(wiki_dir, slug)
    if not path.is_file():
        raise WikiError(f"wiki 页面不存在：{slug}.md")
    page = _parse_page(path, wiki_dir)
    path.unlink()
    return {"path": page["path"], "title": page["title"], "deleted": True}


def refresh_wiki(project_info: dict) -> dict[str, Any]:
    pages = list_wiki_pages(project_info)
    lint = lint_wiki(project_info)
    return {"project": project_info["name"], "page_count": len(pages), "pages": pages, "lint": lint}


def lint_wiki(project_info: dict) -> dict[str, Any]:
    wiki_dir = _wiki_dir(project_info)
    issues: list[dict[str, str]] = []
    if not wiki_dir.exists():
        return {"ok": True, "issues": []}
    for path in sorted(wiki_dir.glob("*.md")):
        try:
            _validate_slug(path.stem)
        except WikiError as exc:
            issues.append({"path": path.name, "code": "invalid_path", "message": str(exc)})
        page = _parse_page(path, wiki_dir)
        if not page["has_metadata"]:
            issues.append({"path": path.name, "code": "missing_metadata", "message": "缺少 frontmatter 元数据。"})
        if not page["title"].strip():
            issues.append({"path": path.name, "code": "empty_title", "message": "标题为空。"})
        if _contains_secret(page["body"]):
            issues.append({"path": path.name, "code": "secret_like_content", "message": "疑似包含 secret。"})
    return {"ok": not issues, "issues": issues}


def _latest_plan_artifact(project_info: dict) -> Path:
    plan_dir = Path(project_info["path"]).resolve() / ".codepilot" / "plans"
    candidates = [path for path in plan_dir.glob("*.md") if path.is_file()] if plan_dir.exists() else []
    if not candidates:
        raise WikiError("没有可 ingest 的 plan artifact。")
    return sorted(candidates, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)[0]


def _trace_body(project_info: dict, *, task_id: int | None, limit: int) -> tuple[str, dict[str, Any]]:
    from codepilot.commands.trace import collect_trace_events

    events = collect_trace_events(project_info, task_id=task_id, limit=max(1, int(limit or 1)))
    if not events:
        raise WikiError("没有可 ingest 的 trace 事件。")
    lines = [
        f"# Trace Digest: {project_info['name']}",
        "",
        "| Time | Source | Event | Status | Phase | Message | Detail |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for item in events:
        cells = [
            str(item.get("timestamp") or "")[:19],
            str(item.get("source") or ""),
            str(item.get("event") or ""),
            str(item.get("status") or ""),
            str(item.get("phase") or ""),
            str(item.get("message") or "").replace("|", "\\|"),
            str(item.get("detail") or "").replace("|", "\\|").replace("\n", " ")[:500],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    metadata = {
        "related_task": str(task_id or ""),
        "related_session": "",
        "related_workflow": "trace",
    }
    return "\n".join(lines), metadata


def _plan_body(project_info: dict) -> tuple[str, dict[str, Any], Path]:
    path = _latest_plan_artifact(project_info)
    body = path.read_text(encoding="utf-8", errors="replace").strip()
    if not body:
        raise WikiError(f"plan artifact 为空：{path.name}")
    metadata = {
        "related_task": "",
        "related_session": "",
        "related_workflow": "plan",
        "source_path": path.resolve().relative_to(Path(project_info["path"]).resolve()).as_posix(),
    }
    return body, metadata, path


def ingest_wiki(project_info: dict, *, source: str, task_id: int | None = None, limit: int = 30) -> dict[str, Any]:
    source = str(source or "").strip().lower()
    if source == "trace":
        body, metadata = _trace_body(project_info, task_id=task_id, limit=limit)
        title = f"Trace Digest {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
        page = add_wiki_note(
            project_info,
            title=title,
            body=body,
            source="wiki.ingest.trace",
            tags=["ingest", "trace"],
            metadata=metadata,
        )
        return {"project": project_info["name"], "from": source, "page": page, "metadata": metadata}
    if source == "plan":
        body, metadata, plan_path = _plan_body(project_info)
        title_match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else f"Plan Digest {plan_path.stem}"
        page = add_wiki_note(
            project_info,
            title=title,
            body=body,
            source="wiki.ingest.plan",
            tags=["ingest", "plan"],
            metadata=metadata,
        )
        return {"project": project_info["name"], "from": source, "page": page, "metadata": metadata}
    raise WikiError("--from 仅支持 trace 或 plan。")


def _emit_or_raise(ctx: click.Context, command: str, json_mode: bool, exc: Exception) -> None:
    if json_mode:
        emit_json_payload(command, ok=False, data={}, error=str(exc), error_code="wiki_error")
        ctx.exit(1)
    raise click.ClickException(str(exc))


@click.group("wiki")
def wiki_group() -> None:
    """管理项目本地 Markdown wiki。"""


@wiki_group.command("ingest")
@click.option("--from", "source", type=click.Choice(["trace", "plan"], case_sensitive=False), required=True, help="沉淀来源")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--task", "task_id", type=int, help="trace 来源时只沉淀指定任务")
@click.option("--limit", type=int, default=30, show_default=True, help="trace 来源最多沉淀事件数")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def ingest_cmd(ctx: click.Context, source: str, project: str | None, task_id: int | None, limit: int, json_mode: bool) -> None:
    """显式把 trace 或 plan artifact 沉淀为 wiki 页面。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        result = ingest_wiki(project_info, source=source, task_id=task_id, limit=limit)
    except (WikiError, click.ClickException) as exc:
        _emit_or_raise(ctx, "wiki ingest", json_mode, exc)
        return
    if json_mode:
        emit_json_payload("wiki ingest", ok=True, data=result)
        return
    echo(f"[green][OK] 已 ingest 到 wiki：{result['page']['path']}[/green]")


@wiki_group.command("add")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--title", required=True, help="页面标题")
@click.option("--body", required=True, help="页面正文")
@click.option("--slug", help="可选安全文件名，不含 .md")
@click.option("--tag", "tags", multiple=True, help="页面标签，可重复")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def add_cmd(ctx: click.Context, project: str | None, title: str, body: str, slug: str | None, tags: tuple[str, ...], json_mode: bool) -> None:
    """新增一条 wiki note。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        page = add_wiki_note(project_info, title=title, body=body, slug=slug, tags=list(tags))
    except (WikiError, click.ClickException) as exc:
        _emit_or_raise(ctx, "wiki add", json_mode, exc)
        return
    data = {"project": project_info["name"], "page": page}
    if json_mode:
        emit_json_payload("wiki add", ok=True, data=data)
        return
    echo(f"[green][OK] 已写入 wiki 页面：{page['path']}[/green]")


@wiki_group.command("list")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def list_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """列出 wiki 页面。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    pages = list_wiki_pages(project_info)
    data = {"project": project_info["name"], "pages": pages}
    if json_mode:
        emit_json_payload("wiki list", ok=True, data=data)
        return
    if not pages:
        echo("[yellow]暂无 wiki 页面[/yellow]")
        return
    for page in pages:
        click.echo(f"{page['path']}\t{page['title'] or '-'}")


@wiki_group.command("update")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--slug", required=True, help="要更新的安全文件名，不含 .md")
@click.option("--title", help="新的页面标题")
@click.option("--body", help="新的页面正文")
@click.option("--tag", "tags", multiple=True, help="替换页面标签，可重复")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def update_cmd(
    ctx: click.Context,
    project: str | None,
    slug: str,
    title: str | None,
    body: str | None,
    tags: tuple[str, ...],
    json_mode: bool,
) -> None:
    """更新一个 wiki 页面。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        page = update_wiki_page(project_info, slug=slug, title=title, body=body, tags=list(tags) if tags else None)
    except (WikiError, click.ClickException) as exc:
        _emit_or_raise(ctx, "wiki update", json_mode, exc)
        return
    data = {"project": project_info["name"], "page": page}
    if json_mode:
        emit_json_payload("wiki update", ok=True, data=data)
        return
    echo(f"[green][OK] 已更新 wiki 页面：{page['path']}[/green]")


@wiki_group.command("delete")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--slug", required=True, help="要删除的安全文件名，不含 .md")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def delete_cmd(ctx: click.Context, project: str | None, slug: str, json_mode: bool) -> None:
    """删除一个 wiki 页面。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        page = delete_wiki_page(project_info, slug=slug)
    except (WikiError, click.ClickException) as exc:
        _emit_or_raise(ctx, "wiki delete", json_mode, exc)
        return
    data = {"project": project_info["name"], "page": page}
    if json_mode:
        emit_json_payload("wiki delete", ok=True, data=data)
        return
    echo(f"[green][OK] 已删除 wiki 页面：{page['path']}[/green]")


@wiki_group.command("refresh")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def refresh_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """刷新并检查项目本地 wiki 索引视图。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    try:
        project_info = _resolve_project(project)
        data = refresh_wiki(project_info)
    except (WikiError, click.ClickException) as exc:
        _emit_or_raise(ctx, "wiki refresh", json_mode, exc)
        return
    if json_mode:
        emit_json_payload("wiki refresh", ok=True, data=data)
        return
    echo(f"[green][OK] wiki refresh：{data['page_count']} pages[/green]")


@wiki_group.command("query")
@click.argument("query", nargs=-1, required=True)
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def query_cmd(ctx: click.Context, query: tuple[str, ...], project: str | None, json_mode: bool) -> None:
    """按关键词查询 wiki。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    query_text = " ".join(query).strip()
    results = query_wiki(project_info, query_text)
    data = {"project": project_info["name"], "query": query_text, "results": results}
    if json_mode:
        emit_json_payload("wiki query", ok=True, data=data)
        return
    if not results:
        echo("[yellow]未匹配到 wiki 页面[/yellow]")
        return
    for item in results:
        click.echo(f"{item['score']}\t{item['path']}\t{item['title']}")
        if item["summary"]:
            click.echo(f"  {item['summary']}")


@wiki_group.command("lint")
@click.option("--project", "-p", help="项目名称，不指定则按当前目录匹配")
@click.option("--json", "json_mode", is_flag=True, help="JSON 输出")
@click.pass_context
def lint_cmd(ctx: click.Context, project: str | None, json_mode: bool) -> None:
    """检查 wiki 页面元数据和路径安全。"""
    json_mode = resolve_json_mode(ctx, json_mode)
    project_info = _resolve_project(project)
    result = lint_wiki(project_info)
    data = {"project": project_info["name"], **result}
    if json_mode:
        emit_json_payload("wiki lint", ok=True, data=data)
        return
    if result["ok"]:
        echo("[green][OK] wiki lint 通过[/green]")
        return
    for issue in result["issues"]:
        click.echo(f"{issue['path']}\t{issue['code']}\t{issue['message']}")
