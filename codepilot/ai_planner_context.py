"""Rich project-context collector used by the planner.

Replaces the original bare-bones ``_collect_project_context`` (which only listed
top-level directories) with a multi-section summary the planner can actually
reason over:

* README excerpt — what the project *says* it is.
* Directory tree (2 levels, filenames only) — what lives where.
* Recent git log — where the codebase is moving.
* Files matched against the requirement keywords — prior-art hits so the
  planner doesn't have to guess file paths.
* Tech-stack hints from manifest files.

The output is *strictly bounded* (default ~4000 chars) so it can be embedded in
a planner prompt without blowing the context budget.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

from codepilot.text_decode import decode_subprocess_text


_SKIP_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "dist",
    "build",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".next",
    "target",
    "coverage",
    ".nyc_output",
}

_CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".java", ".kt", ".rb", ".php", ".cs", ".cpp", ".c",
    ".h", ".hpp", ".swift", ".m", ".mm",
    ".vue", ".svelte", ".md", ".sql", ".toml", ".yaml", ".yml",
}

# Common stop-words that shouldn't drive file-matching.
_STOPWORDS_EN = {
    "the", "and", "for", "that", "with", "this", "from", "into",
    "can", "not", "but", "all", "will", "have", "are", "was",
    "then", "just", "one", "also", "use", "using", "some",
    "about", "what", "which", "how", "been", "more", "when",
    "add", "make", "fix", "new", "old", "any", "our",
}
_STOPWORDS_ZH = {
    "一个", "添加", "新增", "修改", "改成", "改为", "实现",
    "做一个", "做成", "功能", "需要", "请", "帮我", "我",
    "优化", "重构", "完善", "改进", "支持", "使用", "可以",
    "能够", "应该", "应", "进行", "进入", "让", "给",
}


def _run_git(args: list[str], cwd: Path, timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=False,
            timeout=timeout,
        )
        return decode_subprocess_text(result.stdout).strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _extract_keywords(title: str) -> list[str]:
    """Pull meaningful keywords out of a natural-language requirement title.

    Splits on whitespace and CJK punctuation first so phrases like "优化任务规划，加入澄清反问"
    produce distinct chunks ("优化任务规划", "加入澄清反问") and then sliding 2-char
    Chinese bigrams ("优化", "任务", "规划") that can match path tokens like
    ``codepilot/commands/auto.py``.
    """
    if not title:
        return []

    segments = re.split(r"[\s，。；、！？,.:;!?()（）\[\]【】\"'`]+", title)
    seen: set[str] = set()
    keywords: list[str] = []

    def _add(token: str) -> None:
        low = token.lower()
        if not token.strip():
            return
        if low in _STOPWORDS_EN or token in _STOPWORDS_ZH:
            return
        if low in seen:
            return
        seen.add(low)
        keywords.append(token)

    for seg in segments:
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", seg):
            _add(token)
        cjk_run = "".join(re.findall(r"[\u4e00-\u9fff]", seg))
        if len(cjk_run) >= 2:
            _add(cjk_run)
        for i in range(len(cjk_run) - 1):
            _add(cjk_run[i:i + 2])

    return keywords[:16]


def _readme_excerpt(project_path: Path, *, max_chars: int = 800) -> str:
    candidates = [
        project_path / "README.md",
        project_path / "README.zh-CN.md",
        project_path / "README.zh.md",
        project_path / "README.txt",
        project_path / "README",
    ]
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        text = text.strip()
        if not text:
            continue
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n…（已截断）"
        return f"# {path.name} 摘录\n{text}"
    return ""


def _directory_tree(project_path: Path, *, max_items: int = 80) -> str:
    """Two-level directory tree, names only."""
    lines: list[str] = []
    try:
        top_entries = sorted(project_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception:
        return ""
    count = 0
    for entry in top_entries:
        if entry.name.startswith(".") or entry.name in _SKIP_DIR_NAMES:
            continue
        lines.append(f"{entry.name}{'/' if entry.is_dir() else ''}")
        count += 1
        if count >= max_items:
            break
        if entry.is_dir():
            try:
                sub_entries = sorted(entry.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except Exception:
                continue
            sub_count = 0
            for sub in sub_entries:
                if sub.name.startswith(".") or sub.name in _SKIP_DIR_NAMES:
                    continue
                marker = "/" if sub.is_dir() else ""
                lines.append(f"  {sub.name}{marker}")
                sub_count += 1
                count += 1
                if sub_count >= 10 or count >= max_items:
                    break
    return "\n".join(lines)


def _recent_commits(project_path: Path, *, limit: int = 15) -> str:
    if not (project_path / ".git").exists():
        return ""
    log = _run_git(
        ["log", f"-n{limit}", "--pretty=format:%h %s"],
        project_path,
    )
    return log


def _iter_code_files(project_path: Path) -> Iterable[Path]:
    for path in project_path.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _CODE_EXTS:
            continue
        parts_lower = {p.lower() for p in path.parts}
        if parts_lower & _SKIP_DIR_NAMES:
            continue
        yield path


def _match_relevant_files(
    project_path: Path,
    keywords: list[str],
    *,
    max_files: int = 12,
    max_scan: int = 4000,
) -> list[str]:
    """Rank files by how many keywords appear in their path."""
    if not keywords:
        return []

    lowered = [kw.lower() for kw in keywords]
    scored: list[tuple[int, str]] = []
    scanned = 0
    for path in _iter_code_files(project_path):
        scanned += 1
        if scanned > max_scan:
            break
        try:
            rel = path.relative_to(project_path).as_posix()
        except ValueError:
            continue
        rel_low = rel.lower()
        score = sum(1 for kw in lowered if kw in rel_low)
        if score:
            scored.append((score, rel))

    scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return [rel for _, rel in scored[:max_files]]


# ── Project conventions (AGENTS.md / CLAUDE.md / CONTRIBUTING.md / ...) ──

# Probe order matters: first hit wins per slot, so AI-focused docs rank above
# general contributor guides (the AI-focused ones usually contain rules that
# directly bind the planner, e.g. "every PR must add tests").
_CONVENTION_PROBES: tuple[tuple[Path, ...], ...] = (
    (Path("AGENTS.md"),),
    (Path("AGENTS.zh-CN.md"), Path("AGENTS.zh.md")),
    (Path("CLAUDE.md"),),
    (Path("CLAUDE.zh-CN.md"), Path("CLAUDE.zh.md")),
    (Path(".cursor/rules.md"), Path(".cursorrules")),
    (Path(".github/copilot-instructions.md"),),
    (Path("CONTRIBUTING.md"),),
    (Path("CONTRIBUTING.zh-CN.md"), Path("CONTRIBUTING.zh.md")),
    (Path(".clauderules"),),
)


def _read_project_conventions(
    project_path: Path,
    *,
    max_chars_per_file: int = 1200,
    total_cap: int = 3000,
) -> str:
    """Collect convention rules from AGENTS.md / CLAUDE.md / CONTRIBUTING.md / etc.

    Returns a single markdown block concatenating the probes that hit, with
    each source clearly labelled. Caps per-file and total bytes so we don't
    blow the planner's context window on a single long CONTRIBUTING.md.
    """
    if not project_path.exists():
        return ""

    blocks: list[str] = []
    total = 0
    seen_resolved: set[str] = set()

    for probe_group in _CONVENTION_PROBES:
        for rel in probe_group:
            candidate = project_path / rel
            if not candidate.is_file():
                continue
            resolved = str(candidate.resolve())
            if resolved in seen_resolved:
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            text = text.strip()
            if not text:
                continue
            seen_resolved.add(resolved)
            if len(text) > max_chars_per_file:
                text = text[:max_chars_per_file].rstrip() + "\n…（已截断）"
            block = f"### {rel.as_posix()}\n{text}"
            projected = total + len(block) + 2
            if projected > total_cap:
                # Fit what's left.
                remaining = total_cap - total
                if remaining < 200:
                    break
                truncated = block[:remaining].rstrip() + "\n…（达到约定部分总上限）"
                blocks.append(truncated)
                total = total_cap
                break
            blocks.append(block)
            total += len(block) + 2
            break  # only one hit per probe_group
        if total >= total_cap:
            break

    return "\n\n".join(blocks)


def validate_recon_payload(
    payload: dict,
    project_path: str | Path,
    *,
    title: str = "",
    min_fallback_files: int = 2,
) -> tuple[dict, list[str]]:
    """Strip hallucinated paths from a recon result and top it up with real hits.

    Any entry in ``relevant_files`` that doesn't exist on disk is dropped.
    When the cleaned list ends up thin (``< min_fallback_files``) and *title*
    is provided, we supplement it with keyword-matched real files so the
    downstream planner still has concrete paths to latch onto.

    Returns ``(cleaned_payload, dropped_paths)`` — the caller can surface the
    dropped list through its progress callback.
    """
    if not isinstance(payload, dict):
        return {}, []

    proj = Path(project_path) if project_path else None
    cleaned = dict(payload)
    raw_files = cleaned.get("relevant_files") or []
    if not isinstance(raw_files, list):
        raw_files = []

    kept: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for entry in raw_files:
        if not isinstance(entry, str):
            continue
        path_str = entry.strip()
        if not path_str:
            continue
        # Normalize to POSIX relative string.
        normalized = path_str.replace("\\", "/").lstrip("./")
        if normalized in seen:
            continue
        seen.add(normalized)
        if proj is not None and (proj / normalized).is_file():
            kept.append(normalized)
        else:
            dropped.append(normalized)

    if proj is not None and title and len(kept) < min_fallback_files:
        keywords = _extract_keywords(title)
        if keywords:
            for candidate in _match_relevant_files(proj, keywords, max_files=6):
                if candidate in seen:
                    continue
                seen.add(candidate)
                kept.append(candidate)
                if len(kept) >= min_fallback_files + 2:
                    break

    cleaned["relevant_files"] = kept
    return cleaned, dropped


def _tech_stack_hints(project_path: Path) -> list[str]:
    hints: list[str] = []
    probes = [
        ("pyproject.toml", "Python (pyproject.toml)"),
        ("requirements.txt", "Python (requirements.txt)"),
        ("package.json", "Node.js (package.json)"),
        ("Cargo.toml", "Rust (Cargo.toml)"),
        ("go.mod", "Go (go.mod)"),
        ("pom.xml", "Java/Maven"),
        ("build.gradle", "Java/Gradle"),
        ("build.gradle.kts", "Kotlin/Gradle"),
        ("Gemfile", "Ruby"),
        ("composer.json", "PHP"),
        ("pubspec.yaml", "Dart/Flutter"),
        ("Podfile", "iOS / Swift"),
    ]
    for filename, label in probes:
        if (project_path / filename).exists():
            hints.append(label)
    return hints


def collect_planner_context(
    project_path: str,
    requirement_title: str = "",
    *,
    max_chars: int = 4000,
) -> str:
    """Build a structured, token-bounded context block for the planner."""
    if not project_path:
        return ""

    proj = Path(project_path)
    if not proj.exists():
        return ""

    keywords = _extract_keywords(requirement_title) if requirement_title else []

    sections: list[str] = []

    tech = _tech_stack_hints(proj)
    if tech:
        sections.append("## 技术栈\n" + "、".join(tech))

    conventions = _read_project_conventions(proj)
    if conventions:
        sections.append(
            "## 项目约定（来自 AGENTS.md / CLAUDE.md / CONTRIBUTING.md 等，规划时必须遵守）\n"
            + conventions
        )

    tree = _directory_tree(proj)
    if tree:
        sections.append("## 目录结构（两层，仅名称）\n" + tree)

    readme = _readme_excerpt(proj)
    if readme:
        sections.append("## README\n" + readme)

    commits = _recent_commits(proj)
    if commits:
        sections.append("## 最近提交\n" + commits)

    if keywords:
        matches = _match_relevant_files(proj, keywords)
        if matches:
            sections.append(
                "## 需求相关文件（按关键词匹配到的路径，未读内容）\n"
                + "\n".join(matches)
            )
        sections.append("## 识别到的关键词\n" + ", ".join(keywords))

    if not sections:
        return ""

    header = (
        "以下是当前项目的结构化概览，供你规划时参考。"
        "请把它当成事实依据，不要凭空捏造不存在的文件或目录。"
    )
    body = header + "\n\n" + "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n…（上下文已截断）"
    return body
