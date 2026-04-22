"""Signal collection helpers for the `inspect` command."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tokenize
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from codepilot import db


InspectSignalCollectorScope = Literal["project_name", "project_path"]
InspectSignalCollector = Callable[[object], str]
_COLLECTOR_ARG_GETTER_BY_SCOPE: dict[InspectSignalCollectorScope, Callable[[str, Path], object]] = {
    "project_name": lambda project_name, _project_path: project_name,
    "project_path": lambda _project_name, project_path: project_path,
}


@dataclass(frozen=True)
class InspectSignalSpec:
    """Canonical signal definition decoupled from command-layer dispatch."""

    key: str
    title: str
    order: int
    aliases: tuple[str, ...]
    collector_key: str
    collector_scope: InspectSignalCollectorScope = "project_path"


@dataclass(frozen=True)
class InspectSignalResult:
    """Unified signal result model used by prompt aggregation."""

    key: str
    title: str
    order: int
    enabled: bool
    content: str


def normalize_signal_tokens(signals: Iterable[str]) -> set[str]:
    return {signal.strip().lower() for signal in signals if isinstance(signal, str) and signal.strip()}


def is_signal_enabled(spec: InspectSignalSpec, requested_tokens: set[str]) -> bool:
    return bool(requested_tokens & set(spec.aliases))


def collect_signal_content(
    spec: InspectSignalSpec,
    *,
    enabled: bool,
    project_name: str,
    project_path: Path,
    collectors_by_key: Mapping[str, InspectSignalCollector],
    skipped_signal: str,
) -> str:
    if not enabled:
        return skipped_signal
    collector = collectors_by_key[spec.collector_key]
    arg = _COLLECTOR_ARG_GETTER_BY_SCOPE[spec.collector_scope](project_name, project_path)
    return collector(arg)


def collect_signal_results(
    project_name: str,
    project_path: Path,
    *,
    signals: tuple[str, ...],
    specs: Iterable[InspectSignalSpec],
    collectors_by_key: Mapping[str, InspectSignalCollector],
    skipped_signal: str,
) -> list[InspectSignalResult]:
    requested = normalize_signal_tokens(signals)
    results: list[InspectSignalResult] = []
    for spec in specs:
        enabled = is_signal_enabled(spec, requested)
        results.append(
            InspectSignalResult(
                key=spec.key,
                title=spec.title,
                order=spec.order,
                enabled=enabled,
                content=collect_signal_content(
                    spec,
                    enabled=enabled,
                    project_name=project_name,
                    project_path=project_path,
                    collectors_by_key=collectors_by_key,
                    skipped_signal=skipped_signal,
                ),
            )
        )
    return results


def _run_git(args: list[str], cwd: Path, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def collect_git_log(project_path: Path, limit: int = 20) -> str:
    log = _run_git(
        ["log", f"-n{limit}", "--pretty=format:%h %s", "--since=7.days"],
        project_path,
    )
    return log or "（近 7 天无提交）"


def collect_failed_tasks(project: str, limit: int = 10) -> str:
    rows = [
        t
        for t in db.list_tasks(project=project)
        if t["status"] in {"failed", "cancelled"}
    ][:limit]
    if not rows:
        return "（无）"
    lines = []
    for t in rows:
        err = (t.get("error_message") or "").strip().splitlines()
        err_head = err[0] if err else ""
        lines.append(f"#{t['id']} [{t['status']}] {t['title']}  {err_head}")
    return "\n".join(lines)


_TODO_RE = re.compile(
    r"\b(?:TODO|FIXME)\b[:：]?\s*.{0,120}|(?<![.`\[\(])\bXXX\b(?:[:：]|\s+).{0,120}",
    re.IGNORECASE,
)
_MD_TODO_RE = re.compile(
    r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)?(?:\[[ xX]\]\s*)?"
    r"(?:TODO|FIXME)\b[:：]?\s*.{0,120}|"
    r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)?(?:\[[ xX]\]\s*)?(?<![.`\[\(])XXX\b(?:[:：]|\s+).{0,120}",
    re.IGNORECASE,
)
_SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".md"}
_CODE_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".php",
    ".rb",
    ".swift",
    ".scala",
    ".sh",
    ".ps1",
}
_LANG_BY_EXT = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".c": "C/C++",
    ".cc": "C/C++",
    ".cpp": "C/C++",
    ".h": "C/C++",
    ".hpp": "C/C++",
    ".cs": "C#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".ps1": "PowerShell",
}
_COMMENT_MARKERS_BY_EXT = {
    ".py": ("#",),
    ".js": ("//", "/*", "*"),
    ".jsx": ("//", "/*", "*"),
    ".ts": ("//", "/*", "*"),
    ".tsx": ("//", "/*", "*"),
    ".go": ("//", "/*", "*"),
    ".rs": ("//", "/*", "*"),
    ".java": ("//", "/*", "*"),
    ".kt": ("//", "/*", "*"),
    ".c": ("//", "/*", "*"),
    ".cc": ("//", "/*", "*"),
    ".cpp": ("//", "/*", "*"),
    ".h": ("//", "/*", "*"),
    ".hpp": ("//", "/*", "*"),
    ".cs": ("//", "/*", "*"),
    ".php": ("//", "/*", "*", "#"),
    ".rb": ("#",),
    ".swift": ("//", "/*", "*"),
    ".scala": ("//", "/*", "*"),
    ".sh": ("#",),
    ".ps1": ("#",),
}
_COMPLEXITY_RE_BY_EXT = {
    ".py": re.compile(r"\b(if|elif|for|while|except|with|assert|and|or|case)\b"),
    ".js": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".jsx": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".ts": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".tsx": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".go": re.compile(r"\b(if|for|case|select|&&|\|\|)\b"),
    ".rs": re.compile(r"\b(if|for|while|match|&&|\|\|)\b"),
    ".java": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".kt": re.compile(r"\b(if|for|while|when|catch|&&|\|\||\?)\b"),
    ".c": re.compile(r"\b(if|else\s+if|for|while|case|&&|\|\||\?)\b"),
    ".cc": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".cpp": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".h": re.compile(r"\b(if|else\s+if|for|while|case|&&|\|\||\?)\b"),
    ".hpp": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".cs": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".php": re.compile(r"\b(if|elseif|for|foreach|while|case|catch|&&|\|\||\?)\b"),
    ".rb": re.compile(r"\b(if|elsif|unless|for|while|rescue|case|&&|\|\|)\b"),
    ".swift": re.compile(r"\b(if|for|while|case|catch|guard|&&|\|\||\?)\b"),
    ".scala": re.compile(r"\b(if|for|while|case|catch|&&|\|\|)\b"),
    ".sh": re.compile(r"\b(if|for|while|case|elif|&&|\|\|)\b"),
    ".ps1": re.compile(r"\b(if|elseif|foreach|for|while|switch|catch|-and|-or)\b", re.IGNORECASE),
}


def _find_unquoted_marker(line: str, markers: tuple[str, ...]) -> int:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        for marker in markers:
            if line.startswith(marker, index):
                return index
    return -1


def _todo_match_in_code_comment(path: Path, line: str) -> re.Match[str] | None:
    """Return TODO-like markers only when they appear in source comments."""
    match = _TODO_RE.search(line)
    if not match:
        return None

    markers = _COMMENT_MARKERS_BY_EXT.get(path.suffix)
    if not markers:
        return None
    todo_pos = match.start()
    marker_pos = _find_unquoted_marker(line, markers)
    return match if marker_pos != -1 and marker_pos <= todo_pos else None


def _todo_match_in_markdown(line: str, in_fenced_block: bool) -> re.Match[str] | None:
    """Keep documentation TODOs intentional and ignore prose/code examples."""
    if in_fenced_block:
        return None
    stripped = line.strip()
    if stripped.startswith("<!--") and "-->" in stripped:
        return _TODO_RE.search(stripped)
    return _MD_TODO_RE.search(stripped)


def _collect_python_todos(path: Path, project_path: Path, limit: int) -> list[str]:
    hits: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for token in tokenize.generate_tokens(fh.readline):
                if token.type != tokenize.COMMENT:
                    continue
                m = _TODO_RE.search(token.string)
                if not m:
                    continue
                rel = path.relative_to(project_path)
                hits.append(f"{rel}:{token.start[0]}  {m.group(0).strip()}")
                if len(hits) >= limit:
                    break
    except Exception:
        return []
    return hits


def collect_todos(project_path: Path, limit: int = 20) -> str:
    hits: list[str] = []
    for path in project_path.rglob("*"):
        if len(hits) >= limit:
            break
        if not path.is_file() or path.suffix not in _SCAN_EXTS:
            continue
        parts = {p.lower() for p in path.parts}
        if parts & {"node_modules", ".git", "dist", "build", ".venv", "venv", "__pycache__"}:
            continue
        if path.suffix == ".py":
            hits.extend(_collect_python_todos(path, project_path, limit - len(hits)))
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                in_markdown_fence = False
                for lineno, line in enumerate(fh, 1):
                    if path.suffix == ".md":
                        stripped = line.lstrip()
                        if stripped.startswith("```") or stripped.startswith("~~~"):
                            in_markdown_fence = not in_markdown_fence
                            continue
                        m = _todo_match_in_markdown(line, in_markdown_fence)
                    else:
                        m = _todo_match_in_code_comment(path, line)
                    if m:
                        rel = path.relative_to(project_path)
                        hits.append(f"{rel}:{lineno}  {m.group(0).strip()}")
                        if len(hits) >= limit:
                            break
        except Exception:
            continue
    return "\n".join(hits) if hits else "（无）"


def _which(cmd: str) -> Optional[str]:
    import shutil

    return shutil.which(cmd)


def collect_ruff(project_path: Path, limit: int = 30) -> str:
    """Run ruff in report-only mode if available."""
    if not _which("ruff"):
        return "（ruff 未安装，跳过）"
    try:
        result = subprocess.run(
            ["ruff", "check", ".", "--output-format", "concise", "--quiet"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return "（ruff 无发现）"
        return "\n".join(lines[:limit])
    except Exception as exc:
        return f"（ruff 执行失败：{exc}）"


def collect_pytest_collect(project_path: Path, limit: int = 30) -> str:
    """Run pytest --collect-only to surface collection errors and test count."""
    if not _which("pytest"):
        return "（pytest 未安装，跳过）"
    if not (project_path / "tests").exists() and not any(project_path.glob("test_*.py")):
        return "（未发现测试目录，跳过）"
    try:
        result = subprocess.run(
            ["pytest", "--collect-only", "-q"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        output = (result.stdout or "") + (result.stderr or "")
        lines = [ln for ln in output.splitlines() if ln.strip()]
        if not lines:
            return "（pytest collect 无输出）"
        interesting = [
            ln
            for ln in lines
            if "error" in ln.lower() or "warning" in ln.lower() or "test" in ln.lower()
        ]
        picked = interesting[:limit] if interesting else lines[-limit:]
        return "\n".join(picked)
    except Exception as exc:
        return f"（pytest collect 失败：{exc}）"


def _skip_scan_path(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return bool(parts & {"node_modules", ".git", "dist", "build", ".venv", "venv", "__pycache__"})


def _manifest_lock_status(manifest: Path, locks: tuple[str, ...]) -> str:
    existing = [manifest.parent / lock for lock in locks if (manifest.parent / lock).exists()]
    if not existing:
        return "missing"
    newest_lock = max(existing, key=lambda item: item.stat().st_mtime)
    return "stale" if manifest.stat().st_mtime > newest_lock.stat().st_mtime else "ok"


def _count_unpinned_requirements(path: Path) -> int:
    count = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(("-", "--")):
            continue
        if not any(op in line for op in ("==", ">=", "<=", "~=", ">", "<")):
            count += 1
    return count


def collect_dependency_health(project_path: Path, limit: int = 20) -> str:
    """Collect cheap, offline dependency health signals without hitting registries."""
    findings: list[str] = []
    package_manifests = [p for p in project_path.rglob("package.json") if p.is_file() and not _skip_scan_path(p)]
    for package_json in package_manifests:
        if len(findings) >= limit:
            break
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        has_dependencies = bool(data.get("dependencies") or data.get("devDependencies") or data.get("optionalDependencies"))
        if not has_dependencies:
            continue
        rel = package_json.relative_to(project_path)
        status = _manifest_lock_status(package_json, ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"))
        if status == "missing":
            findings.append(f"{rel}: 发现依赖但缺少 lockfile")
        elif status == "stale":
            findings.append(f"{rel}: package.json 比 lockfile 更新，可能需要刷新依赖锁")

    for req in project_path.rglob("requirements*.txt"):
        if len(findings) >= limit:
            break
        if not req.is_file() or _skip_scan_path(req):
            continue
        unpinned = _count_unpinned_requirements(req)
        if unpinned:
            rel = req.relative_to(project_path)
            findings.append(f"{rel}: {unpinned} 个依赖未固定版本")

    return "\n".join(findings[:limit]) if findings else "（无明显依赖健康问题；未联网检查最新版本）"


def _code_line_count(text: str, suffix: str) -> int:
    markers = _COMMENT_MARKERS_BY_EXT.get(suffix, ())
    count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if markers and any(line.startswith(marker) for marker in markers):
            continue
        count += 1
    return count


def _generic_complexity_score(text: str, suffix: str) -> int:
    pattern = _COMPLEXITY_RE_BY_EXT.get(suffix)
    if not pattern:
        return 0
    lines = []
    markers = _COMMENT_MARKERS_BY_EXT.get(suffix, ())
    for raw in text.splitlines():
        line = raw.strip()
        if not line or (markers and any(line.startswith(marker) for marker in markers)):
            continue
        lines.append(line)
    return len(pattern.findall("\n".join(lines)))


def _python_complexity_for_function(node: ast.AST) -> int:
    score = 1

    def visit(current: ast.AST) -> None:
        nonlocal score
        for child in ast.iter_child_nodes(current):
            if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.Assert)):
                score += 1
            elif isinstance(child, ast.BoolOp):
                score += max(1, len(child.values) - 1)
            elif isinstance(child, ast.Match):
                score += max(1, len(child.cases))
            elif isinstance(child, ast.comprehension):
                score += 1 + len(child.ifs)
            visit(child)

    visit(node)
    return score


def _python_function_complexities(path: Path, project_path: Path, text: str, threshold: int) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    rel = path.relative_to(project_path).as_posix()
    hits: list[tuple[int, str, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            name = ".".join([*self.scope, node.name])
            complexity = _python_complexity_for_function(node)
            if complexity >= threshold:
                hits.append((complexity, f"{rel}:{node.lineno}", name))
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

    Visitor().visit(tree)
    return hits


def collect_code_metrics(project_path: Path, limit: int = 20) -> str:
    """Collect offline code size and rough complexity signals."""
    totals: dict[str, dict[str, int]] = {}
    largest: list[tuple[int, int, str]] = []
    complex_files: list[tuple[int, int, str]] = []
    complex_functions: list[tuple[int, str, str]] = []
    total_files = 0
    total_code_lines = 0

    for path in project_path.rglob("*"):
        if not path.is_file() or path.suffix not in _CODE_EXTS or _skip_scan_path(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = path.relative_to(project_path).as_posix()
        lang = _LANG_BY_EXT.get(path.suffix, path.suffix.lstrip(".").upper())
        code_lines = _code_line_count(text, path.suffix)
        complexity = _generic_complexity_score(text, path.suffix)
        total_files += 1
        total_code_lines += code_lines
        bucket = totals.setdefault(lang, {"files": 0, "lines": 0})
        bucket["files"] += 1
        bucket["lines"] += code_lines
        largest.append((code_lines, complexity, rel))
        if complexity >= 20:
            complex_files.append((complexity, code_lines, rel))
        if path.suffix == ".py":
            complex_functions.extend(_python_function_complexities(path, project_path, text, threshold=8))

    if not total_files:
        return "（未发现可扫描的代码文件）"

    largest.sort(reverse=True)
    complex_files.sort(reverse=True)
    complex_functions.sort(reverse=True)

    lines = [f"总体：{total_files} 个代码文件，约 {total_code_lines} 行有效代码"]
    lines.append("语言分布：")
    for lang, stat in sorted(totals.items(), key=lambda item: item[1]["lines"], reverse=True)[:8]:
        lines.append(f"- {lang}: {stat['files']} 文件 / {stat['lines']} 行")

    lines.append("大文件：")
    for code_lines, complexity, rel in largest[: min(5, limit)]:
        if code_lines <= 0:
            continue
        lines.append(f"- {rel}: {code_lines} 行，分支复杂度约 {complexity}")

    if complex_functions:
        lines.append("复杂函数（Python，估算圈复杂度）：")
        for complexity, location, name in complex_functions[: min(5, limit)]:
            lines.append(f"- {location} {name}: 复杂度约 {complexity}")

    if complex_files:
        lines.append("复杂文件（跨语言粗略估算）：")
        for complexity, code_lines, rel in complex_files[: min(5, limit)]:
            lines.append(f"- {rel}: 分支复杂度约 {complexity} / {code_lines} 行")

    return "\n".join(lines)
