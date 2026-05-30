"""Code size and complexity signal collector helpers."""

from __future__ import annotations

import ast
from pathlib import Path

from codepilot.commands.inspect_signal_collectors_shared import (
    CODE_EXTS,
    COMMENT_MARKERS_BY_EXT,
    COMPLEXITY_RE_BY_EXT,
    LANG_BY_EXT,
    should_skip_scan_path,
)


def _code_line_count(text: str, suffix: str) -> int:
    markers = COMMENT_MARKERS_BY_EXT.get(suffix, ())
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
    pattern = COMPLEXITY_RE_BY_EXT.get(suffix)
    if not pattern:
        return 0
    lines = []
    markers = COMMENT_MARKERS_BY_EXT.get(suffix, ())
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
            if child is not node and isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
            ):
                continue
            if isinstance(
                child,
                (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.Assert),
            ):
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


def _python_function_complexities(
    path: Path,
    project_path: Path,
    text: str,
    threshold: int,
) -> list[tuple[int, str, str]]:
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


def is_test_file_path(rel_path: str) -> bool:
    """Return whether a repo-relative path is test-only surface."""
    normalized = str(rel_path or "").replace("\\", "/").strip().lower()
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized
    stem = Path(name).stem
    return (
        any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts[:-1])
        or name.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
    )


def _new_metric_bucket() -> dict[str, object]:
    return {
        "totals": {},
        "largest": [],
        "complex_files": [],
        "complex_functions": [],
        "files": 0,
        "lines": 0,
    }


def _add_metric(
    bucket: dict[str, object],
    *,
    lang: str,
    code_lines: int,
    complexity: int,
    rel: str,
    function_hits: list[tuple[int, str, str]],
) -> None:
    totals = bucket["totals"]
    assert isinstance(totals, dict)
    stat = totals.setdefault(lang, {"files": 0, "lines": 0})
    stat["files"] += 1
    stat["lines"] += code_lines
    bucket["files"] = int(bucket["files"]) + 1
    bucket["lines"] = int(bucket["lines"]) + code_lines
    largest = bucket["largest"]
    assert isinstance(largest, list)
    largest.append((code_lines, complexity, rel))
    if complexity >= 20:
        complex_files = bucket["complex_files"]
        assert isinstance(complex_files, list)
        complex_files.append((complexity, code_lines, rel))
    complex_functions = bucket["complex_functions"]
    assert isinstance(complex_functions, list)
    complex_functions.extend(function_hits)


def _sort_metric_bucket(bucket: dict[str, object]) -> None:
    for key in ("largest", "complex_files", "complex_functions"):
        values = bucket[key]
        assert isinstance(values, list)
        values.sort(reverse=True)


def _render_metric_bucket(
    lines: list[str],
    bucket: dict[str, object],
    *,
    label: str,
    limit: int,
    advisory_suffix: str = "",
) -> None:
    totals = bucket["totals"]
    largest = bucket["largest"]
    complex_files = bucket["complex_files"]
    complex_functions = bucket["complex_functions"]
    assert isinstance(totals, dict)
    assert isinstance(largest, list)
    assert isinstance(complex_files, list)
    assert isinstance(complex_functions, list)

    if int(bucket["files"]) <= 0:
        lines.append(f"{label}：未发现可报告热点{advisory_suffix}")
        return

    lines.append(f"语言分布（{label}）：")
    for lang, stat in sorted(totals.items(), key=lambda item: item[1]["lines"], reverse=True)[:8]:
        lines.append(f"- {lang}: {stat['files']} 文件 / {stat['lines']} 行")

    lines.append(f"{label}大文件{advisory_suffix}：")
    for code_lines, complexity, rel in largest[: min(5, limit)]:
        if code_lines <= 0:
            continue
        lines.append(f"- {rel}: {code_lines} 行，分支复杂度约 {complexity}")

    if complex_functions:
        lines.append(f"{label}复杂函数{advisory_suffix}（Python，估算圈复杂度）：")
        for complexity, location, name in complex_functions[: min(5, limit)]:
            lines.append(f"- {location} {name}: 复杂度约 {complexity}")

    if complex_files:
        lines.append(f"{label}复杂文件{advisory_suffix}（跨语言粗略估算）：")
        for complexity, code_lines, rel in complex_files[: min(5, limit)]:
            lines.append(f"- {rel}: 分支复杂度约 {complexity} / {code_lines} 行")


def collect_code_metrics(project_path: Path, limit: int = 20) -> str:
    """Collect offline code size and rough complexity signals."""
    production = _new_metric_bucket()
    tests = _new_metric_bucket()
    total_files = 0
    total_code_lines = 0

    for path in project_path.rglob("*"):
        if not path.is_file() or path.suffix not in CODE_EXTS or should_skip_scan_path(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = path.relative_to(project_path).as_posix()
        lang = LANG_BY_EXT.get(path.suffix, path.suffix.lstrip(".").upper())
        code_lines = _code_line_count(text, path.suffix)
        complexity = _generic_complexity_score(text, path.suffix)
        total_files += 1
        total_code_lines += code_lines
        function_hits: list[tuple[int, str, str]] = []
        if path.suffix == ".py":
            function_hits = _python_function_complexities(path, project_path, text, threshold=8)
        bucket = tests if is_test_file_path(rel) else production
        _add_metric(
            bucket,
            lang=lang,
            code_lines=code_lines,
            complexity=complexity,
            rel=rel,
            function_hits=function_hits,
        )

    if not total_files:
        return "（未发现可扫描的代码文件）"

    _sort_metric_bucket(production)
    _sort_metric_bucket(tests)

    lines = [f"总体：{total_files} 个代码文件，约 {total_code_lines} 行有效代码"]
    lines.append(f"生产代码：{production['files']} 个文件，约 {production['lines']} 行有效代码")
    lines.append(f"测试代码：{tests['files']} 个文件，约 {tests['lines']} 行有效代码（仅参考，不单独触发任务）")
    _render_metric_bucket(lines, production, label="生产", limit=limit)
    lines.append("测试文件（仅参考，不单独触发任务）：")
    _render_metric_bucket(
        lines,
        tests,
        label="测试",
        limit=limit,
        advisory_suffix="（仅参考，不单独触发任务）",
    )

    return "\n".join(lines)
