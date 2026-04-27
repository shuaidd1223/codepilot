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


def collect_code_metrics(project_path: Path, limit: int = 20) -> str:
    """Collect offline code size and rough complexity signals."""
    totals: dict[str, dict[str, int]] = {}
    largest: list[tuple[int, int, str]] = []
    complex_files: list[tuple[int, int, str]] = []
    complex_functions: list[tuple[int, str, str]] = []
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
        bucket = totals.setdefault(lang, {"files": 0, "lines": 0})
        bucket["files"] += 1
        bucket["lines"] += code_lines
        largest.append((code_lines, complexity, rel))
        if complexity >= 20:
            complex_files.append((complexity, code_lines, rel))
        if path.suffix == ".py":
            complex_functions.extend(
                _python_function_complexities(path, project_path, text, threshold=8)
            )

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
